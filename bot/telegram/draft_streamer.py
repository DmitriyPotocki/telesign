"""DraftStreamer — streams LLM text into Telegram live drafts (sendMessageDraft).

Throttling: two-tier token bucket (min interval ~0.9 s AND min N new chars between
frames) to respect Telegram limits (20 calls/5 s, 40 calls/30 s per chat).
On TelegramRetryAfter: sleep, skip intermediate frames, always send the last frame.
Fallback: if drafts are unavailable -> send_message + edit_message_text.
Final text is always sent as a regular send_message (drafts are ephemeral, ~30 s).
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)

from .formatting import clean_output, limit_text, md_to_html, strip_html_for_fallback

logger = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 4096


class DraftStreamer:
    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        draft_id: int | None = None,
        min_interval: float = 0.9,
        min_delta_chars: int = 24,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.draft_id = draft_id or (secrets.randbelow(2**31 - 1) + 1)
        self.min_interval = min_interval
        self.min_delta_chars = min_delta_chars

        self._full_text = ""  # accumulated raw markdown
        self._sent_html = ""  # what we last rendered to Telegram
        self._last_send: float | None = None
        self._available = True  # drafts supported?
        self._fallback_msg: int | None = None
        self._lock = asyncio.Lock()

    # --- internals --------------------------------------------------------
    async def _push_draft(self, html_text: str) -> None:
        try:
            await self.bot.send_message_draft(
                chat_id=self.chat_id,
                draft_id=self.draft_id,
                text=html_text,
                parse_mode="HTML",
                can_stop=False,
            )
        except TelegramRetryAfter as e:
            # sleep as requested; intermediate frames are skipped by throttling logic
            logger.info("draft FLOOD_WAIT %s -> sleeping %.1fs", self.chat_id, e.retry_after)
            await asyncio.sleep(e.retry_after + 0.1)
            return
        except (TelegramBadRequest, TelegramNotFound) as e:
            # method not available / old Bot API -> permanent fallback
            logger.warning("sendMessageDraft unavailable, falling back to send/edit: %s", e)
            self._available = False
            await self._fallback_send(html_text)

    async def _fallback_send(self, html_text: str) -> None:
        try:
            msg = await self.bot.send_message(
                chat_id=self.chat_id, text=html_text, parse_mode="HTML"
            )
            self._fallback_msg = msg.message_id
        except TelegramBadRequest:
            # markup rejected -> send raw text
            plain = strip_html_for_fallback(self._full_text)
            msg = await self.bot.send_message(chat_id=self.chat_id, text=plain)
            self._fallback_msg = msg.message_id

    async def _fallback_edit(self, html_text: str) -> None:
        if self._fallback_msg is None:
            await self._fallback_send(html_text)
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id, message_id=self._fallback_msg, text=html_text, parse_mode="HTML"
            )
        except TelegramBadRequest:
            try:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id, message_id=self._fallback_msg,
                    text=strip_html_for_fallback(self._full_text),
                )
            except TelegramBadRequest as e:
                logger.warning("fallback edit failed: %s", e)

    async def _should_send(self, html_text: str) -> bool:
        """Two-tier token bucket: time AND size."""
        now = time.monotonic()
        if self._last_send is None:
            return True
        since = now - self._last_send
        added = len(html_text) - len(self._sent_html)
        if since < self.min_interval:
            return False
        return added >= self.min_delta_chars or since >= self.min_interval * 3

    async def _render(self, *, force: bool = False, cleaned: str | None = None) -> None:
        if not self._full_text and not force:
            return
        # partial markdown -> valid html
        source = cleaned if cleaned is not None else clean_output(self._full_text)
        rendered = md_to_html(source, partial=True) or ""
        rendered = limit_text(rendered, TELEGRAM_TEXT_LIMIT)
        if not force and not await self._should_send(rendered):
            return
        self._sent_html = rendered
        self._last_send = time.monotonic()
        if self._available:
            await self._push_draft(rendered)
        else:
            await self._fallback_edit(rendered)

    # --- public API -------------------------------------------------------
    async def feed(self, chunk: str) -> None:
        if not chunk:
            return
        async with self._lock:
            self._full_text += chunk
            try:
                # clean on every render so a service block spanning several
                # chunks is removed as soon as it closes
                await self._render(cleaned=clean_output(self._full_text))
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 0.1)
            except TelegramForbiddenError:
                logger.warning("chat %s blocked the bot", self.chat_id)
                raise
            except (TelegramBadRequest, TelegramNotFound) as e:
                logger.warning("draft render error: %s", e)

    async def status(self, text: str) -> None:
        """Progress status shown as a draft ('Анализирую бриф…', 'Генерирую 3/6…')."""
        async with self._lock:
            self._full_text = text
            try:
                await self._render(force=True, cleaned=clean_output(text))
            except (TelegramRetryAfter, TelegramBadRequest, TelegramNotFound) as e:
                logger.info("status draft skipped: %s", e)

    async def finalize(self, final_text: str | None = None) -> None:
        """Finish the stream: always send a regular message with the full text."""
        if final_text:
            self._full_text = final_text
        text = limit_text(clean_output(self._full_text), TELEGRAM_TEXT_LIMIT) if self._full_text else ""
        if not text.strip():
            self._full_text = ""
            return
        html_text = md_to_html(text)
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=html_text, parse_mode="HTML")
        except TelegramBadRequest:
            try:
                await self.bot.send_message(chat_id=self.chat_id, text=strip_html_for_fallback(text))
            except TelegramBadRequest as e:
                logger.error("finalize send failed: %s", e)
        except TelegramForbiddenError:
            logger.warning("chat %s blocked the bot", self.chat_id)
        # next draft will use a new id; and reset state
        self.draft_id = secrets.randbelow(2**31 - 1) + 1
        self._full_text = ""
        self._sent_html = ""
        self._fallback_msg = None
