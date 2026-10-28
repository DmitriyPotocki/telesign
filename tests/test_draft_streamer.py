import asyncio
import types

from aiogram.exceptions import TelegramRetryAfter

from bot.telegram.draft_streamer import DraftStreamer


class FakeBot:
    def __init__(self, fail_drafts=False, flood_once=False):
        self.drafts = []
        self.sent = []
        self.fail_drafts = fail_drafts
        self.flood_once = flood_once
        self._flooded = False
        self.edits = []

    async def send_message_draft(self, **kw):
        from aiogram.exceptions import TelegramBadRequest

        if self.fail_drafts:
            raise TelegramBadRequest(method=None, message="Bad Request: unknown method sendMessageDraft")
        if self.flood_once and not self._flooded:
            self._flooded = True
            raise TelegramRetryAfter(method=None, message="Flood", retry_after=0.05)
        self.drafts.append(kw["text"])

    async def send_message(self, **kw):
        self.sent.append(kw)
        return types.SimpleNamespace(message_id=1)

    async def edit_message_text(self, **kw):
        self.edits.append(kw)


def run(coro):
    return asyncio.run(coro)


def test_throttling_limits_rate(monkeypatch):
    async def go():
        bot = FakeBot()
        s = DraftStreamer(bot, 1, draft_id=7, min_interval=0.05, min_delta_chars=50)
        # feed many chunks very fast
        for i in range(30):
            await s.feed(f"word{i} ")
        await s.finalize()
        return bot, len(bot.drafts)

    bot, n = run(go())
    # 30 rapid chunks must NOT produce 30 draft calls
    assert n < 15
    assert len(bot.sent) == 1  # exactly one final message


def test_final_always_sent():
    async def go():
        bot = FakeBot()
        s = DraftStreamer(bot, 1, draft_id=7)
        await s.feed("Привет ")
        await asyncio.sleep(0.0)
        await s.feed("мир")
        await s.finalize()
        return bot

    bot = run(go())
    assert bot.sent[-1]["text"] == "Привет мир"
    # first frame is shown immediately (no prior frame), but nothing leaks after finalize
    assert bot.drafts == ["Привет"]  # cleaned of the trailing space


def test_flood_wait_retries_and_last_frame():
    async def go():
        bot = FakeBot(flood_once=True)
        s = DraftStreamer(bot, 1, draft_id=7, min_interval=0.0, min_delta_chars=0)
        await s.feed("первый кадр")
        assert bot.drafts == []  # flood ate it
        await s.feed(" последний кадр")
        return bot, s

    bot, _s = run(go())
    # after FLOOD_WAIT the next frame goes through
    assert bot.drafts
    assert "последний" in bot.drafts[-1]


def test_fallback_to_send_edit():
    async def go():
        bot = FakeBot(fail_drafts=True)
        s = DraftStreamer(bot, 1, draft_id=7, min_interval=0.0, min_delta_chars=0)
        await s.feed("первый")
        await s.feed("-второй")
        await s.finalize()
        return bot

    bot = run(go())
    # fallback: one send + edits, plus final message
    assert bot.edits or bot.sent
    assert len(bot.sent) >= 2


def test_finalize_empty_is_noop():
    async def go():
        bot = FakeBot()
        s = DraftStreamer(bot, 1)
        await s.finalize()
        return bot

    assert run(go()).sent == []


def test_status_shows_draft():
    async def go():
        bot = FakeBot()
        s = DraftStreamer(bot, 1, draft_id=9, min_interval=0.0)
        await s.status("Анализирую бриф…")
        return bot

    bot = run(go())
    assert bot.drafts and "Анализирую" in bot.drafts[-1]
