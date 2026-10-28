"""/selfcheck command: run self-check and report to the user."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services.context import AppContext
from ..services.selfcheck import SelfCheck

router = Router(name="selfcheck")


@router.message(Command("selfcheck"))
async def cmd_selfcheck(message: Message, ctx: AppContext) -> None:
    # whitelist is already enforced by middleware; extra guard for clarity
    if message.from_user.id not in ctx.cfg.whitelist:
        await message.answer("⛔ Самопроверка доступна только из whitelist.")
        return
    msg = await message.answer("🔍 Запускаю самопроверку…")
    checker = SelfCheck(
        message.bot, ctx.cfg, ctx.db, ctx.client, ctx.transcriber
    )
    checks = await checker.run()
    lines = ["<b>Самопроверка</b>", ""]
    for sym, line in checks:
        lines.append(f"{sym} {line}")
    ok = all(sym == "✅" for sym, _ in checks)
    lines.append("")
    lines.append("Всё в порядке ✅" if ok else "Есть замечания — смотри ⚠️/❌.")
    try:
        await msg.edit_text("\n".join(lines), parse_mode="HTML")
    except Exception:
        await message.answer("\n".join(lines), parse_mode="HTML")
