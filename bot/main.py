"""Startup, middleware, routers, graceful shutdown."""
from __future__ import annotations

import asyncio
import logging
import pathlib
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from .config import get_config
from .db.repo import Database
from .handlers import (
    common,
    files_cmd,
    history,
    instructs,
    messages,
    selfcheck_cmd,
    settings,
)
from .handlers.middleware import WhitelistMiddleware
from .llm.openrouter import OpenRouter
from .services.context import AppContext
from .services.files import FileStorage
from .services.pipeline import Pipeline
from .services.selfcheck import SelfCheck, startup_log
from .services.userqueue import ChatQueue
from .stt.base import make_transcriber
from .telegram.draft_streamer import DraftStreamer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
# never log secrets
logging.getLogger("aiogram.client.session").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)


def make_pipeline_factory(db, cfg, client, storage):
    def factory(streamer: DraftStreamer) -> Pipeline:
        return Pipeline(db, cfg, client, storage, streamer)

    return factory


async def main() -> None:
    cfg = get_config()
    if not cfg.bot_token:
        logger.error("BOT_TOKEN is empty — set .env")
        return

    pathlib.Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)

    db = Database(pathlib.Path(cfg.data_dir) / "bot.sqlite3")
    await db.connect()
    await db.migrate()

    client = OpenRouter(cfg.openrouter_api_key, cfg.openrouter_base_url)
    storage = FileStorage(db, cfg)
    transcriber = make_transcriber(cfg)
    queue = ChatQueue()

    bot = Bot(token=cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    ctx = AppContext(
        cfg=cfg,
        db=db,
        client=client,
        storage=storage,
        transcriber=transcriber,
        queue=queue,
        pipeline_factory=make_pipeline_factory(db, cfg, client, storage),
    )
    dp["ctx"] = ctx

    # whitelist on message/edited_message/callback updates
    dp.message.middleware(WhitelistMiddleware(cfg))
    dp.edited_message.middleware(WhitelistMiddleware(cfg))  # edited updates are a separate kind
    dp.callback_query.middleware(WhitelistMiddleware(cfg))

    dp.include_router(common.router)
    dp.include_router(instructs.router)
    dp.include_router(history.router)
    dp.include_router(files_cmd.router)
    dp.include_router(settings.router)
    dp.include_router(selfcheck_cmd.router)
    dp.include_router(messages.router)  # catch-all last

    # commands menu
    from aiogram.types import BotCommand

    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Справка"),
                BotCommand(command="help", description="Справка"),
                BotCommand(command="instructs", description="Постоянные инструкции"),
                BotCommand(command="new", description="Новый проект"),
                BotCommand(command="history", description="Шаги и откат"),
                BotCommand(command="files", description="Файлы"),
                BotCommand(command="settings", description="Настройки"),
                BotCommand(command="selfcheck", description="Самопроверка"),
                BotCommand(command="cancel", description="Отмена генерации"),
            ]
        )
    except Exception:
        logger.warning("set_my_commands failed", exc_info=True)

    # startup self-check (log only)
    try:
        checks = await SelfCheck(bot, cfg, db, client, transcriber).run()
        await startup_log(checks)
    except Exception:
        logger.exception("startup selfcheck failed")

    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        # graceful shutdown: cancel tracked pipeline tasks
        for chat_id in list(queue._tasks.keys()):
            task = queue._tasks.get(chat_id)
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*[t for t in queue._tasks.values() if not t.done()], return_exceptions=True)
        await client.close()
        await db.close()
        logger.info("shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
