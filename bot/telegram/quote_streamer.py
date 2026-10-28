"""QuoteStreamer — streams LLM text into a persistent <blockquote> message.

Unlike DraftStreamer (ephemeral drafts), quote blocks are ordinary messages
edited in place: used by the viewer phase to show "viewing image" headers and
the reasoning that follows, each as its own block in the chat.

Throttling: min interval + min new chars between edits (Telegram allows ~20
edits / 5 s per chat). "message is not modified" is benign; missing/broken
messages disable further edits instead of raising.
"""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNotFound,
    TelegramRetryAfter,
)

from .formatting import clean_output, limit_text, md_to_html, strip_html_for_fallback

logger = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 4096
QUOTE_MARGIN = 80  # <blockquote> wrapper + tags overhead


class QuoteStreamer:
    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        min_interval: float = 1.2,
        min_delta_chars: int = 40,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.min_interval = min_interval
        self.min_delta_chars = min_delta_chars

        self._full_text = ""
        self._sent_html = ""
        self._msg_id: int | None = None
        self._last_send: float | None = None
        self._plain = False  # fell back to plain text (no HTML)
        self._broken = False  # message gone/uneditable — stop trying
        self._lock = asyncio.Lock()

    # --- rendering ---------------------------------------------------------
    def _render(self) -> str:
        body = md_to_html(clean_output(self._full_text), partial=True) or ""
        body = limit_text(body, TELEGRAM_TEXT_LIMIT - QUOTE_MARGIN)
        if self._plain:
            return body
        return f"<blockquote>{body}</blockquote>"

    # --- public API ---------------------------------------------------------
    async def start(self, header: str) -> None:
        """Send the block right away with its static header line."""
        self._full_text = header
        html = self._render()
        try:
            msg = await self.bot.send_message(
                chat_id=self.chat_id, text=html, parse_mode="HTML"
            )
            self._msg_id = msg.message_id
        except TelegramBadRequest:
            # markup rejected -> plain message, keep editing it plainly
            self._plain = True
            msg = await self.bot.send_message(
                chat_id=self.chat_id, text=strip_html_for_fallback(self._full_text)
            )
            self._msg_id = msg.message_id
        self._last_send = time.monotonic()
        self._sent_html = html

    async def feed(self, chunk: str) -> None:
        """Append a streamed piece; edits are throttled."""
        if not chunk or self._broken:
            return
        async with self._lock:
            self._full_text += chunk
            html = self._render()
            if not self._should_edit(html):
                return
            await self._edit(html)

    async def finalize(self) -> None:
        """Always land the final text (one last edit, bypasses throttling)."""
        if self._broken:
            return
        async with self._lock:
            if self._msg_id is None:
                # never started: still show the block
                if self._full_text:
                    await self.start(self._full_text)
                return
            html = self._render()
            if html != self._sent_html:
                await self._edit(html, final=True)

    # --- internals -----------------------------------------------------------
    def _should_edit(self, html: str) -> bool:
        now = time.monotonic()
        if self._last_send is None:
            return True
        since = now - self._last_send
        if since < self.min_interval:
            return False
        added = len(html) - len(self._sent_html)
        return added >= self.min_delta_chars or since >= self.min_interval * 3

    async def _edit(self, html: str, *, final: bool = False) -> None:
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self._msg_id,
                text=html,
                parse_mode=None if self._plain else "HTML",
            )
            self._sent_html = html
            self._last_send = time.monotonic()
        except TelegramRetryAfter as e:
            if final:
                # the final frame must land: wait out the flood and retry once
                logger.info("quote final edit FLOOD_WAIT %.1fs -> sleeping", e.retry_after)
                await asyncio.sleep(e.retry_after + 0.1)
                try:
                    await self.bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=self._msg_id,
                        text=html,
                        parse_mode=None if self._plain else "HTML",
                    )
                    self._sent_html = html
                    self._last_send = time.monotonic()
                except Exception as e2:
                    logger.warning("quote final edit failed: %s", e2)
                    self._broken = True
                return
            logger.info("quote edit FLOOD_WAIT %.1fs, skipping frame", e.retry_after)
            # keep text; finalize() will try once more after the caller finishes
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                self._last_send = time.monotonic()
                return
            logger.warning("quote edit failed: %s", e)
            self._broken = True
        except TelegramNotFound:
            logger.warning("quote message %s gone", self._msg_id)
            self._broken = True
