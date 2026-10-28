"""/files: saved files by category with delete buttons."""
from __future__ import annotations

import logging
import pathlib

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..services.context import AppContext

logger = logging.getLogger(__name__)
router = Router(name="files")

DEL_PREFIX = "file:del:"
KINDS = {
    "mold": "Молды",
    "texture": "Текстуры",
    "reference": "Референсы",
    "result": "Результаты",
    "voice": "Голосовые",
}
KIND_TITLES = {v: k for k, v in KINDS.items()}


@router.message(Command("files"))
async def cmd_files(message: Message, ctx: AppContext) -> None:
    user_id = message.from_user.id
    kb = [[InlineKeyboardButton(text=title, callback_data=f"files:kind:{kind}")]
          for kind, title in KINDS.items()]
    counts = []
    for kind in KINDS:
        rows = await ctx.db.list_files(user_id, kind=kind, limit=1000)
        counts.append(f"{KINDS[kind]}: {len(rows)}")
    await message.answer(
        "<b>Сохранённые файлы</b>\n" + "\n".join(counts) +
        "\n\nОткрой категорию, чтобы просмотреть и удалить.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )


@router.callback_query(F.data.startswith("files:kind:"))
async def cb_kind(cb: CallbackQuery, ctx: AppContext) -> None:
    kind = cb.data.removeprefix("files:kind:")
    if kind not in KINDS:
        await cb.answer()
        return
    rows = await ctx.db.list_files(cb.from_user.id, kind=kind, limit=20)
    kb = [
        [InlineKeyboardButton(text=f"🗑 #{r.id} {r.caption or ''}".strip()[:60],
                              callback_data=f"{DEL_PREFIX}{r.id}")]
        for r in rows
    ]
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="files:back")])
    await cb.message.answer(
        f"<b>{KINDS[kind]}</b> ({len(rows)} шт.)", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else None,
    )
    # full-quality documents (Telegram recompresses answer_photo hard)
    for r in rows[:6]:
        p = pathlib.Path(r.path)
        if p.exists() and (r.mime or "").startswith("image/"):
            try:
                await cb.message.answer_document(
                    FSInputFile(r.path),
                    caption=f"#{r.id}" + (f" — {r.caption}" if r.caption else ""),
                )
            except Exception:
                logger.exception("doc send failed")
    await cb.answer()


@router.callback_query(F.data == "files:back")
async def cb_back(cb: CallbackQuery, ctx: AppContext) -> None:
    await cmd_files(cb.message, ctx)  # type: ignore[arg-type]
    await cb.answer()


@router.callback_query(F.data.startswith(DEL_PREFIX))
async def cb_del(cb: CallbackQuery, ctx: AppContext) -> None:
    try:
        file_id = int(cb.data.removeprefix(DEL_PREFIX))
    except ValueError:
        await cb.answer()
        return
    row = await ctx.db.delete_file(cb.from_user.id, file_id)
    if row:
        p = pathlib.Path(row.path)
        if p.exists():
            try:
                p.unlink()
            except Exception:
                logger.exception("unlink failed")
    await cb.answer("Удалено" if row else "Не найдено")
    # refresh category view
    if row:
        try:
            kb = [[InlineKeyboardButton(text=f"📂 {KINDS[row.kind]}",
                                        callback_data=f"files:kind:{row.kind}")]]
            await cb.message.answer("Файл удалён.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        except Exception:
            pass
