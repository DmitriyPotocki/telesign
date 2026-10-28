"""Main scenario handlers: photos/albums, text briefs, voice briefs -> pipeline."""
from __future__ import annotations

import asyncio
import logging
import pathlib
import tempfile

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message

from ..services.context import AppContext
from ..services.files import role_from_caption
from ..services.pipeline import Pipeline, peek_base_choice, pop_base_choice
from ..telegram.albums import SINGLE, AlbumCollector, best_photo_file_id
from ..telegram.draft_streamer import DraftStreamer

logger = logging.getLogger(__name__)
router = Router(name="messages")

collector = AlbumCollector()


async def _notify_wait(bot: Bot, chat_id: int) -> None:
    try:
        await bot.send_message(chat_id, "⏳ Предыдущий запрос ещё выполняется — поставил в очередь.")
    except Exception:
        pass


async def _run_pipeline(
    ctx: AppContext,
    bot: Bot,
    chat_id: int,
    user_id: int,
    brief: str,
    *,
    attached: list[int] | None = None,
    base_override: int | None = None,
    offer_base_choice: bool = False,
) -> None:
    streamer = DraftStreamer(bot, chat_id)
    pipeline: Pipeline = ctx.make_pipeline(streamer)
    try:
        async with ctx.queue.slot(chat_id, notify_wait=lambda: _notify_wait(bot, chat_id)):
            ctx.queue.track(chat_id, asyncio.current_task())  # type: ignore[arg-type]
            await pipeline.run(
                user_id,
                chat_id,
                bot,
                brief,
                attached=attached,
                base_override=base_override,
                offer_base_choice=offer_base_choice,
            )
    except asyncio.CancelledError:
        await streamer.finalize("Генерация отменена (/cancel).")
        raise
    except Exception:
        logger.exception("pipeline failed")
        await streamer.finalize("Что-то сломалось при генерации. Попробуй ещё раз или /cancel.")
    finally:
        task = asyncio.current_task()
        if task:
            ctx.queue.untrack(chat_id, task)


def _clean_caption(caption: str | None) -> str | None:
    """What gets stored as the file's NAME.

    Role prefixes keep only their label; free text is kept only when short.
    The full prompt must never become the name of a texture/reference —
    the model names images itself (viewer phase / gallery).
    """
    if not caption:
        return None
    kind, label = role_from_caption(caption)
    if kind is not None:
        return (label or "").strip() or None
    text = caption.strip()
    return text if len(text) <= 40 else None


def _brief_from_caption(caption: str | None) -> str:
    """One-stage flow: the caption IS the brief. Role prefixes are stripped."""
    if not caption:
        return ""
    kind, label = role_from_caption(caption)
    if kind is not None:
        return (label or "").strip()
    return caption.strip()


# --- photos / documents / albums ---------------------------------------------
@router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def on_photo(message: Message, ctx: AppContext) -> None:
    file_id = best_photo_file_id(message)
    if file_id is None:
        await message.answer("Не понял картинку. Пришли фото или image-файл.")
        return
    added = await collector.add(message)
    if added is None:
        return  # part of an album that already has a waiter
    if added == SINGLE:
        await _save_items(ctx, message, [(file_id, message.caption)], single=True)
        return
    # first message of an album: spawn one waiter
    album = await collector.wait_album(added)

    async def _later() -> None:
        await _save_items(
            ctx, message, [(i.file_id, i.caption) for i in album.items], single=False
        )

    asyncio.get_running_loop().create_task(_later())


async def _save_items(
    ctx: AppContext, message: Message, items: list[tuple[str, str | None]], *, single: bool
) -> None:
    ids: list[int] = []
    molds = 0
    brief = ""
    warnings: list[str] = []
    for file_id, caption in items:
        try:
            res = await _save_one(ctx, message, file_id, caption)
        except Exception:
            logger.exception("save image failed")
            res = None
        if res is None:
            warnings.append("⚠️ не удалось сохранить файл (слишком большой?)")
            continue
        file_db_id, kind = res
        ids.append(file_db_id)
        if kind == "mold":
            molds += 1
        if not brief:
            brief = _brief_from_caption(caption)

    if warnings:
        await message.answer("\n".join(warnings))

    if brief:
        # one-stage: images + prompt in one message -> run right away.
        # A single attached photo is passed along so an edit targets it.
        await _run_pipeline(
            ctx,
            message.bot,
            message.chat.id,
            message.from_user.id,
            brief,
            attached=ids[:1] if (single and ids) else None,
        )
        return

    # no prompt yet: show the tool-call style viewing blocks (cached per file),
    # then ask for the brief — never dump ✅-lines with user captions
    files = [
        {"id": f.id, "kind": f.kind, "caption": f.caption, "analysis": f.analysis}
        for f in await ctx.db.list_files(message.from_user.id, limit=50)
    ]
    try:
        viewer = ctx.make_pipeline(None)  # streamer unused by the viewer phase
        await viewer._view_materials(message.from_user.id, message.chat.id, message.bot, files)
    except Exception:
        logger.exception("viewer phase failed on upload")
    if molds:
        await message.answer("Пришли бриф текстом или голосом.")
    else:
        await message.answer("Принял. Пришли бриф, когда будет готово.")



async def _save_one(ctx: AppContext, message: Message, file_id: str, caption: str | None):
    from ..imaging.metrics import resize_image
    from ..services.role import RoleClassifier

    kind, _label = role_from_caption(caption)
    if kind is None:
        # LLM decides by the picture
        got = await ctx.storage.download(message.bot, file_id)
        if got is None:
            return None
        data, mime = got
        classifier = RoleClassifier(ctx.client, ctx.cfg)
        kind = await classifier.classify(resize_image(data, max_side=768), hint=caption)
        # store the original bytes untouched; model inputs are resized at use time
        return await ctx.storage.store_bytes(
            message.from_user.id, data, mime, kind, _clean_caption(caption),
            tg_file_id=file_id,
        )
    return await ctx.storage.save_image(
        message.from_user.id, message.bot, file_id, _clean_caption(caption),
        default_kind=kind
    )


# --- voice briefs -------------------------------------------------------------
@router.message(F.voice | F.audio)
async def on_voice(message: Message, ctx: AppContext, bot: Bot) -> None:
    media = message.voice or message.audio
    duration = media.duration or 0
    if duration > ctx.cfg.max_voice_seconds:
        await message.answer(f"Голосовое длиннее {ctx.cfg.max_voice_seconds} с. Разбей на части.")
        return
    got = await ctx.storage.download(bot, media.file_id)
    if got is None:
        await message.answer("Не смог скачать аудио (слишком большое?).")
        return
    data, mime = got
    tmp = pathlib.Path(tempfile.mkstemp(suffix=".ogg")[1])
    tmp.write_bytes(data)
    await message.answer("🎙 Расшифровываю…")
    try:
        text = await ctx.transcriber.transcribe(tmp, mime)
    except Exception:
        logger.exception("transcription failed")
        await message.answer("Не получилось расшифровать голосовое. Напиши текстом.")
        tmp.unlink(missing_ok=True)
        return
    tmp.unlink(missing_ok=True)
    if not text:
        await message.answer("Голосовое пустое или не распозналось. Попробуй ещё раз.")
        return
    # save the original voice file for the history
    try:
        await ctx.storage.store_bytes(message.from_user.id, data, "audio/ogg", "voice", None,
                                      tg_file_id=media.file_id)
    except Exception:
        logger.exception("voice file save failed")
    await message.answer(f"🎙 «{text}»")
    await _run_pipeline(
        ctx, bot, message.chat.id, message.from_user.id, text, offer_base_choice=True
    )


# --- text briefs --------------------------------------------------------------
@router.message(F.text)
async def on_text(message: Message, ctx: AppContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return  # unknown commands are ignored (registered commands already handled)
    # no image attached -> the pipeline offers the numbered gallery first
    await _run_pipeline(
        ctx, bot, message.chat.id, message.from_user.id, text, offer_base_choice=True
    )


# --- edited messages: re-run the agent on the edited text ----------------------
@router.edited_message(F.text)
async def on_edited_text(message: Message, ctx: AppContext, bot: Bot) -> None:
    """The user corrected their brief -> answer it again from scratch."""
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return  # edited commands are ignored; empty edits are meaningless
    logger.info("message %s edited, re-running pipeline", message.message_id)
    await _run_pipeline(
        ctx, bot, message.chat.id, message.from_user.id, text, offer_base_choice=True
    )


# --- result buttons -----------------------------------------------------------
@router.callback_query(F.data.in_({"more", "rollback", "continue_here"}))
async def on_result_buttons(cb: CallbackQuery, ctx: AppContext, bot: Bot) -> None:
    if cb.data == "more":
        await cb.answer("Пришли следующий бриф — он будет поверх этого результата.", show_alert=True)
    elif cb.data == "rollback":
        from .history import show_history

        await show_history(cb.message, ctx)
        await cb.answer()
    elif cb.data == "continue_here":
        await cb.answer("Ок, следующая генерация пойдёт от этого шага.")


# --- base choice after the gallery ---------------------------------------------
@router.callback_query(F.data.startswith("editbase:"))
async def on_edit_base_choice(cb: CallbackQuery, ctx: AppContext, bot: Bot) -> None:
    """User tapped «Править №N» -> run the stored brief against that file."""
    parts = cb.data.split(":")
    if len(parts) != 3:
        await cb.answer()
        return
    _, token, num = parts
    try:
        number = int(num)
    except ValueError:
        await cb.answer()
        return
    entry = peek_base_choice(token)
    if entry is None:
        await cb.answer("Выбор устарел — пришли бриф ещё раз.", show_alert=True)
        return
    file_id = entry["mapping"].get(number)
    if file_id is None:
        await cb.answer("Нет такого номера.", show_alert=True)
        return
    pop_base_choice(token)
    await cb.answer("Правлю выбранное изображение…")
    await _run_pipeline(
        ctx,
        bot,
        cb.message.chat.id,
        cb.from_user.id,
        entry["brief"],
        base_override=file_id,
    )
