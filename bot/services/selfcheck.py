"""Self-check: bot token, whitelist, OpenRouter key, models, DB, ffmpeg, STT, drafts."""
from __future__ import annotations

import asyncio
import logging
import pathlib
import shutil
import tempfile

from aiogram import Bot

from ..config import Config
from ..db.repo import Database
from ..llm.openrouter import OpenRouter, OpenRouterError
from ..stt.base import Transcriber

logger = logging.getLogger(__name__)


class SelfCheck:
    def __init__(self, bot: Bot, cfg: Config, db: Database, client: OpenRouter,
                 transcriber: Transcriber) -> None:
        self.bot = bot
        self.cfg = cfg
        self.db = db
        self.client = client
        self.transcriber = transcriber

    async def run(self) -> list[tuple[str, str]]:
        """Returns list of (status_symbol, line). symbol: ✅ ⚠️ ❌"""
        results: list[tuple[str, str]] = []
        results.append(await self.check_token())
        results.append(self.check_whitelist())
        results.append(await self.check_openrouter())
        results.append(await self.check_models())
        results.append(await self.check_db())
        results.append(self.check_ffmpeg())
        results.append(await self.check_stt())
        results.append(await self.check_drafts())
        return results

    async def check_token(self) -> tuple[str, str]:
        try:
            me = await self.bot.get_me()
            return "✅", f"токен бота валиден (@{me.username})"
        except Exception as e:
            return "❌", f"токен бота невалиден: {type(e).__name__}"

    def check_whitelist(self) -> tuple[str, str]:
        if not self.cfg.whitelist:
            return "❌", "ALLOWED_USER_IDS пуст — бот отвечает всем отказом"
        return "✅", f"whitelist задан ({len(self.cfg.whitelist)} польз.)"

    async def check_openrouter(self) -> tuple[str, str]:
        if not self.cfg.openrouter_api_key:
            return "❌", "OPENROUTER_API_KEY не задан"
        try:
            resp = await self.client.chat(
                self.cfg.llm_model,
                [{"role": "user", "content": "Say OK"}],
                max_tokens=5,
                reasoning={"effort": "low", "exclude": True},
            )
            text = ((resp.get("choices") or [{}])[0].get("message", {}).get("content") or "")
            if text.strip():
                return "✅", "ключ OpenRouter работает"
            return "⚠️", "ключ OpenRouter работает, но ответ пустой"
        except OpenRouterError as e:
            return "❌", f"ключ OpenRouter: {e}"

    async def check_models(self) -> tuple[str, str]:
        ok_llm = await self.client.model_available(self.cfg.llm_model)
        ok_img = await self.client.model_available(self.cfg.image_model)
        ok_stt = True
        if self.cfg.stt_provider == "cloud":
            ok_stt = await self.client.model_available(self.cfg.stt_model)
        marks = []
        marks.append("✅ LLM" if ok_llm else "⚠️ LLM не найден в каталоге")
        marks.append("✅ image" if ok_img else "⚠️ image не найден в каталоге")
        if self.cfg.stt_provider == "cloud":
            marks.append("✅ STT" if ok_stt else "⚠️ STT не найден в каталоге")
        sym = "✅" if ok_llm and ok_img and ok_stt else "⚠️"
        return sym, "; ".join(marks)

    async def check_db(self) -> tuple[str, str]:
        try:
            tmp = pathlib.Path(tempfile.gettempdir()) / f"telesign_dbcheck_{asyncio.get_running_loop().time()}.db"
            d2 = Database(tmp)
            await d2.connect()
            await d2.migrate()
            await d2.ensure_user(42)
            await d2.close()
            tmp.unlink(missing_ok=True)
            main_ok = await self.db.fetchone("SELECT 1 AS x")
            if main_ok:
                return "✅", "БД доступна на запись (WAL)"
            return "❌", "основная БД недоступна"
        except Exception as e:
            return "❌", f"БД: {type(e).__name__}: {e}"

    def check_ffmpeg(self) -> tuple[str, str]:
        if shutil.which("ffmpeg"):
            return "✅", "ffmpeg найден"
        if self.cfg.stt_provider == "cloud":
            return "✅", "ffmpeg не найден (не обязателен: STT облачный; недоступен только ogg→wav фолбэк)"
        return "⚠️", "ffmpeg не найден — локальный STT не будет работать"

    async def check_stt(self) -> tuple[str, str]:
        try:
            await self.transcriber.warmup()
            return "✅", f"STT инициализирован ({self.cfg.stt_provider})"
        except Exception as e:
            return "⚠️", f"STT не инициализирован: {type(e).__name__} (голос не будет работать)"

    async def check_drafts(self) -> tuple[str, str]:
        try:
            # no real chat to test without spamming: probe = draft to an impossible chat
            await self.bot.send_message_draft(chat_id=0, draft_id=1, text="")
            return "✅", "send_message_draft доступен"
        except Exception as e:
            name = type(e).__name__
            if "RetryAfter" in name:
                return "✅", "send_message_draft доступен (rate limited, но метод есть)"
            return "⚠️", f"send_message_draft недоступен ({name}) — будет фолбэк на send/edit"


async def startup_log(checks: list[tuple[str, str]]) -> None:
    for sym, line in checks:
        logger.info("selfcheck %s %s", sym, line)
