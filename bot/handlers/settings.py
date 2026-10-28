"""/settings: default variants count and aspect ratio."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..services.context import AppContext

router = Router(name="settings")

RATIOS = ["auto", "1:1", "4:3", "3:2", "16:9", "4:5", "2:3", "9:16"]


async def _render(message: Message, ctx: AppContext) -> None:
    settings = await ctx.db.get_settings(message.from_user.id)
    variants = settings.get("variants", ctx.cfg.default_variants)
    ratio = settings.get("aspect_ratio", "auto")
    nvar_kb = [
        [InlineKeyboardButton(text=str(n), callback_data=f"set:variants:{n}")
         for n in (2, 4, 6, 8)]
    ]
    ratio_kb = [
        [InlineKeyboardButton(text=r if r != "auto" else "auto",
                              callback_data=f"set:ratio:{r}") for r in RATIOS[:4]],
        [InlineKeyboardButton(text=r, callback_data=f"set:ratio:{r}") for r in RATIOS[4:]],
    ]
    await message.answer(
        f"<b>Настройки</b>\nВариантов по умолчанию: <b>{variants}</b>\n"
        f"Формат (aspect ratio): <b>{ratio}</b>\n\n"
        "Выбери число вариантов или формат:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=nvar_kb + ratio_kb),
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message, ctx: AppContext) -> None:
    await _render(message, ctx)


@router.callback_query(F.data.startswith("set:variants:"))
async def cb_variants(cb: CallbackQuery, ctx: AppContext) -> None:
    n = int(cb.data.rsplit(":", 1)[1])
    settings = await ctx.db.get_settings(cb.from_user.id)
    settings["variants"] = min(max(n, 1), ctx.cfg.max_variants)
    await ctx.db.save_settings(cb.from_user.id, settings)
    await cb.answer(f"Вариантов по умолчанию: {n}")
    await _render(cb.message, ctx)  # type: ignore[arg-type]


@router.callback_query(F.data.startswith("set:ratio:"))
async def cb_ratio(cb: CallbackQuery, ctx: AppContext) -> None:
    ratio = cb.data.removeprefix("set:ratio:")
    settings = await ctx.db.get_settings(cb.from_user.id)
    settings["aspect_ratio"] = ratio
    await ctx.db.save_settings(cb.from_user.id, settings)
    await cb.answer(f"Формат: {ratio}")
    await _render(cb.message, ctx)  # type: ignore[arg-type]
