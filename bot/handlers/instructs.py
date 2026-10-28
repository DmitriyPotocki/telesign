"""Persistent instructions manager (/instructs)."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..config import Config
from ..services.context import AppContext
from ..services.files import sanitize_text

logger = logging.getLogger(__name__)
router = Router(name="instructs")

ADD_BTN = "instr:add"
DEL_PREFIX = "instr:del:"
CONFIRM_CB = "instr:vc:save"
FIX_CB = "instr:vc:fix"
CANCEL_CB = "instr:vc:cancel"


class InstrStates(StatesGroup):
    waiting_text = State()
    waiting_voice_fix = State()


def _list_kb(rows: list[dict]) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    kb = [[InlineKeyboardButton(text=f"🗑 {i + 1}", callback_data=f"{DEL_PREFIX}{r['id']}")]
          for i, r in enumerate(rows)]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def _main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить", callback_data=ADD_BTN)]]
    )


async def _show_list(message: Message, ctx: AppContext) -> None:
    rows = await ctx.db.list_instructions(message.from_user.id)
    cfg: Config = ctx.cfg
    if not rows:
        await message.answer(
            f"Постоянных инструкций пока нет. Максимум {cfg.max_instructions} по "
            f"{cfg.max_instruction_len} символов.\nНажми «Добавить» или пришли текст.",
            reply_markup=_main_kb(),
        )
        return
    text = "<b>Постоянные инструкции</b>\n\n" + "\n".join(
        f"<b>{i + 1}.</b> {_esc(r['text'])} <i>({r['source']})</i>" for i, r in enumerate(rows)
    )
    kb = _list_kb(rows)
    if kb:
        kb.inline_keyboard.append([InlineKeyboardButton(text="➕ Добавить", callback_data=ADD_BTN)])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


def _esc(s: str) -> str:
    import html

    return html.escape(s, quote=False)


@router.message(Command("instructs"))
async def cmd_instructs(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await _show_list(message, ctx)


@router.callback_query(F.data == ADD_BTN)
async def cb_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(InstrStates.waiting_text)
    await cb.message.answer(
        "Пришли текст инструкции сообщением или голосом. /cancel — отмена."
    )
    await cb.answer()


@router.message(InstrStates.waiting_text, F.text, F.text[0] != "/")
async def add_text(message: Message, ctx: AppContext, state: FSMContext) -> None:
    cfg = ctx.cfg
    text = sanitize_text(message.text or "", cfg.max_instruction_len)
    if not text:
        await message.answer("Пустая инструкция, попробуй ещё раз.")
        return
    if await ctx.db.count_instructions(message.from_user.id) >= cfg.max_instructions:
        await message.answer(f"Лимит {cfg.max_instructions} инструкций. Удали что-то из списка.")
        await state.clear()
        return
    await ctx.db.add_instruction(message.from_user.id, text, "text", None)
    await state.clear()
    await message.answer("✅ Инструкция сохранена.")
    await _show_list(message, ctx)


@router.message(InstrStates.waiting_text, F.voice | F.audio)
async def add_voice(message: Message, ctx: AppContext, state: FSMContext) -> None:
    voice = message.voice or message.audio
    seconds = voice.duration or 0
    if seconds > ctx.cfg.max_voice_seconds:
        await message.answer(f"Голосовое длиннее {ctx.cfg.max_voice_seconds} с — не принимаю.")
        return
    file_id = voice.file_id
    data = await ctx.storage.download(message.bot, file_id)
    if data is None:
        await message.answer("Не смог скачать аудио (слишком большое?).")
        return
    import pathlib
    import tempfile

    path = pathlib.Path(tempfile.mkstemp(suffix=".ogg")[1])
    path.write_bytes(data[0])
    await message.answer("🎙 Расшифровываю…")
    try:
        text = await ctx.transcriber.transcribe(path, "audio/ogg")
    except Exception:
        logger.exception("transcribe failed")
        await message.answer("Не получилось расшифровать аудио. Попробуй текстом.")
        path.unlink(missing_ok=True)
        return
    path.unlink(missing_ok=True)
    if not text:
        await message.answer("Голосовое пустое или не распозналось.")
        return
    text = sanitize_text(text, ctx.cfg.max_instruction_len)

    await state.update_data(voice_text=text, tg_file_id=file_id, seconds=seconds)
    await state.set_state(InstrStates.waiting_voice_fix)
    await message.answer(
        f"🎙 Расшифровка:\n<i>{_esc(text)}</i>\n\nСохранить?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Сохранить", callback_data=CONFIRM_CB),
                    InlineKeyboardButton(text="✏️ Исправить", callback_data=FIX_CB),
                    InlineKeyboardButton(text="✖️ Отмена", callback_data=CANCEL_CB),
                ]
            ]
        ),
        parse_mode="HTML",
    )


@router.callback_query(InstrStates.waiting_voice_fix, F.data == CONFIRM_CB)
async def cb_voice_save(cb: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    text = data.get("voice_text", "")
    if not text:
        await cb.answer("Нет текста", show_alert=True)
        return
    await ctx.db.add_instruction(cb.from_user.id, text, "voice", data.get("tg_file_id"))
    await state.clear()
    await cb.message.answer("✅ Инструкция сохранена.")
    await _show_list(cb.message, ctx)
    await cb.answer()


@router.callback_query(InstrStates.waiting_voice_fix, F.data == FIX_CB)
async def cb_voice_fix(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(InstrStates.waiting_text)
    await cb.message.answer("Пришли исправленный текст инструкции.")
    await cb.answer()


@router.callback_query(F.data == CANCEL_CB)
async def cb_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.answer("Отменено.")
    await cb.answer()


@router.callback_query(F.data.startswith(DEL_PREFIX))
async def cb_delete(cb: CallbackQuery, ctx: AppContext) -> None:
    try:
        instr_id = int(cb.data.removeprefix(DEL_PREFIX))
    except ValueError:
        await cb.answer()
        return
    ok = await ctx.db.delete_instruction(cb.from_user.id, instr_id)
    await cb.answer("Удалено" if ok else "Не найдено")
    await _show_list(cb.message, ctx)


# any other text while in waiting_text state: allow commands to pass through
@router.message(InstrStates.waiting_text, F.text)
async def add_text_cmds(message: Message, ctx: AppContext, state: FSMContext) -> None:
    if message.text.startswith("/"):
        await state.clear()
        await message.answer("Отменено.")
        return
    await add_text(message, ctx, state)
