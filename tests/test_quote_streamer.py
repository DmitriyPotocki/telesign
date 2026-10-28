"""QuoteStreamer: persistent <blockquote> messages with streamed, cleaned text."""
from types import SimpleNamespace

from bot.telegram.quote_streamer import QuoteStreamer


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self._mid = 0

    async def send_message(self, chat_id, text, parse_mode=None):
        self._mid += 1
        self.sent.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
        return SimpleNamespace(message_id=self._mid)

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None):
        self.edited.append({"message_id": message_id, "text": text, "parse_mode": parse_mode})


def _all_text(bot: FakeBot) -> str:
    return "\n".join(m["text"] for m in bot.sent + bot.edited)


async def test_start_sends_blockquote_with_header():
    bot = FakeBot()
    qs = QuoteStreamer(bot, 42, min_interval=0.0, min_delta_chars=0)
    await qs.start("🖼 Смотрю: молд #5 — гостиная\n")

    assert len(bot.sent) == 1
    assert bot.sent[0]["text"].startswith("<blockquote>")
    assert bot.sent[0]["text"].endswith("</blockquote>")
    assert "Смотрю: молд #5" in bot.sent[0]["text"]
    assert bot.sent[0]["parse_mode"] == "HTML"


async def test_streaming_feeds_edits_and_cleans_service_tags():
    bot = FakeBot()
    qs = QuoteStreamer(bot, 42, min_interval=0.0, min_delta_chars=0)
    await qs.start("Заголовок\n")
    await qs.feed("<thinking>секретные мысли</thinking>")
    await qs.feed("Комната с панорамным окном")
    await qs.finalize()

    text = _all_text(bot)
    assert "thinking" not in text and "секретные" not in text  # clean_output applied
    assert "Комната с панорамным окном" in text
    assert bot.edited, "streamed chunks must edit the block in place"
    assert all(m["text"].startswith("<blockquote>") for m in bot.edited)


async def test_partial_markdown_is_closed_in_final_frame():
    bot = FakeBot()
    qs = QuoteStreamer(bot, 42, min_interval=0.0, min_delta_chars=0)
    await qs.start("header\n")
    # markdown bold arrives split across chunks (as streaming does)
    await qs.feed("**жирный")
    await qs.feed("** кусок")
    await qs.finalize()

    final = bot.edited[-1]["text"]
    assert "<b>жирный</b> кусок" in final  # closed once the stream completes


async def test_throttling_blocks_edits_but_finalize_lands():
    bot = FakeBot()
    qs = QuoteStreamer(bot, 42, min_interval=999.0, min_delta_chars=10_000)
    await qs.start("header\n")
    await qs.feed("очень длинный стриминг")
    await qs.feed("ещё кусок")
    assert bot.edited == []  # throttled

    await qs.finalize()
    assert len(bot.edited) == 1
    assert "ещё кусок" in bot.edited[0]["text"]


async def test_finalize_without_start_sends_the_block_once():
    bot = FakeBot()
    qs = QuoteStreamer(bot, 42, min_interval=0.0, min_delta_chars=0)
    await qs.feed("просто текст")
    await qs.finalize()

    assert len(bot.sent) == 1
    assert bot.sent[0]["text"].startswith("<blockquote>")
    assert "просто текст" in bot.sent[0]["text"]
