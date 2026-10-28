"""Whitelist middleware: only ALLOWED_USER_IDS may use the bot."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from ..config import Config

logger = logging.getLogger(__name__)


class WhitelistMiddleware(BaseMiddleware):
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None and isinstance(event, Update):
            for kind in ("message", "callback_query"):
                obj = getattr(event, kind, None)
                if obj is not None:
                    user = getattr(obj, "from_user", None)
                    if user:
                        break
        if user is None or user.id not in self.cfg.whitelist:
            if isinstance(event, Message):
                try:
                    await event.answer("⛔ Доступ только для дизайнеров из whitelist.")
                except Exception:
                    pass
            elif isinstance(event, CallbackQuery):
                try:
                    await event.answer("⛔ Нет доступа", show_alert=True)
                except Exception:
                    pass
            if user is not None:
                logger.info("rejected user %s", user.id)
            return None
        data["allowed_user_id"] = user.id
        return await handler(event, data)
