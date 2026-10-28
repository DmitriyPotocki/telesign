"""Edited messages re-run the agent; whitelist must cover edited updates too."""
from datetime import UTC, datetime

from aiogram.types import Chat, Message, User

from bot.config import Config
from bot.handlers.messages import on_edited_text, router
from bot.handlers.middleware import WhitelistMiddleware


def make_message(text: str | None = "brief", user_id: int = 111) -> Message:
    return Message(
        message_id=7,
        date=datetime.now(UTC),
        chat=Chat(id=777, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="D"),
        text=text,
    )


def test_router_handles_edited_text():
    observer = router.observers.get("edited_message")
    assert observer is not None, "edited_message observer must be registered"
    assert len(observer.handlers) == 1


async def test_edited_command_is_ignored():
    # returns before touching ctx/bot -> proves the "/" guard runs first
    await on_edited_text(make_message(text="/help"), None, None)  # type: ignore[arg-type]


async def test_edited_empty_text_is_ignored():
    await on_edited_text(make_message(text=None), None, None)  # type: ignore[arg-type]


async def test_whitelist_allows_known_user():
    cfg = Config(bot_token="t", openrouter_api_key="k", allowed_user_ids="111")
    mw = WhitelistMiddleware(cfg)
    called = []

    async def handler(event, data):
        called.append(data.get("allowed_user_id"))
        return "ok"

    res = await mw(handler, make_message(user_id=111), {"event_from_user": make_message().from_user})
    assert res == "ok"
    assert called == [111]


async def test_whitelist_blocks_stranger_on_message_event():
    # edited_message events are Message instances -> same middleware path
    cfg = Config(bot_token="t", openrouter_api_key="k", allowed_user_ids="111")
    mw = WhitelistMiddleware(cfg)
    called = []

    async def handler(event, data):
        called.append(1)
        return "ok"

    res = await mw(handler, make_message(user_id=999), {"event_from_user": make_message(user_id=999).from_user})
    assert res is None
    assert called == []
