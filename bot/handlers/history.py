"""/history: steps of the current project with thumbnails and rollback buttons."""
from __future__ import annotations

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

router = Router(name="history")

ROLLBACK_PREFIX = "step:rollback:"
SHOW_PREFIX = "step:show:"

MAX_STEP_PREVIEW = 10


@router.message(Command("history"))
async def cmd_history(message: Message, ctx: AppContext) -> None:
    await show_history(message, ctx)


async def show_history(message: Message, ctx: AppContext) -> None:
    project_id = await ctx.db.get_active_project(message.from_user.id)
    project = await ctx.db.get_project(project_id)
    active_step = (project or {}).get("active_step_id")
    steps = await ctx.db.list_steps(project_id)
    if not steps:
        await message.answer("История пуста. Пришли бриф — появится первый шаг.")
        return
    lines: list[str] = ["<b>Шаги проекта</b>"]
    kb: list[list[InlineKeyboardButton]] = []
    for i, s in enumerate(steps):
        marker = " ← активный" if s.id == active_step else ""
        winner = f", победитель #{s.winner_file_id}" if s.winner_file_id else ""
        flags = []
        if s.quality_note:
            flags.append("качество низкое")
        if s.disputed:
            flags.append("скор спорный")
        fl = f" ({', '.join(flags)})" if flags else ""
        lines.append(f"<b>{i + 1}.</b> #{s.id}: {s.brief_text[:100]}{winner}{fl}{marker}")
        kb.append([InlineKeyboardButton(text=f"↩️ Откатить к шагу {i + 1}",
                                        callback_data=f"{ROLLBACK_PREFIX}{s.id}")])
    kb.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="step:close")])
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

    # full-quality documents (Telegram recompresses answer_photo hard)
    for s in steps[-MAX_STEP_PREVIEW:]:
        if s.winner_file_id:
            row = await ctx.db.get_file(message.from_user.id, s.winner_file_id)
            if row and pathlib.Path(row.path).exists():
                try:
                    await message.answer_document(
                        FSInputFile(row.path),
                        caption=f"Шаг {s.id}: {s.brief_text[:60]}",
                    )
                except Exception:
                    pass


@router.callback_query(F.data.startswith(ROLLBACK_PREFIX))
async def cb_rollback(cb: CallbackQuery, ctx: AppContext) -> None:
    try:
        step_id = int(cb.data.removeprefix(ROLLBACK_PREFIX))
    except ValueError:
        await cb.answer()
        return
    step = await ctx.db.get_step(step_id)
    if not step or step.project_id != await ctx.db.get_active_project(cb.from_user.id):
        await cb.answer("Шаг не найден в текущем проекте", show_alert=True)
        return
    if not step.winner_file_id:
        await cb.answer("У шага нет результата для отката", show_alert=True)
        return
    await ctx.db.set_active_step(step.project_id, step_id)
    await cb.answer(f"Активный шаг — {step_id}. Следующая генерация пойдёт от него.")
    try:
        await cb.message.answer(
            f"Откатился к шагу #{step_id}. Следующий запрос будет поверх его результата."
        )
    except Exception:
        pass


@router.callback_query(F.data == "step:close")
async def cb_close(cb: CallbackQuery) -> None:
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer()
