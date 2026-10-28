import asyncio

from bot.config import Config
from bot.imaging.generate import ImageClient
from bot.services.files import role_from_caption, sanitize_text
from bot.services.userqueue import ChatQueue


def test_role_from_caption():
    assert role_from_caption("текстура: дуб") == ("texture", "дуб")
    assert role_from_caption("молд: гостиная") == ("mold", "гостиная")
    assert role_from_caption("Референс: стиль сканди") == ("reference", "стиль сканди")
    assert role_from_caption("обычный текст без роли") == (None, None)
    assert role_from_caption(None) == (None, None)


def test_sanitize_text():
    assert sanitize_text("  много   пробелов  ") == "много пробелов"
    assert len(sanitize_text("x" * 1000, 500)) == 500


def test_split_materials_more_than_7():
    cfg = Config(bot_token="t", openrouter_api_key="k", max_texture_refs=7)
    mats = [object()] * 9
    def split(materials):
        if len(materials) <= cfg.max_texture_refs:
            return [materials] if materials else [[]]
        out = []
        for i in range(0, len(materials), cfg.max_texture_refs):
            out.append(materials[i:i + cfg.max_texture_refs])
        return out

    groups = split(mats)
    assert len(groups) == 2
    assert len(groups[0]) == 7 and len(groups[1]) == 2


def test_build_prompt_maps_references():
    cfg = Config(bot_token="t", openrouter_api_key="k")
    ic = ImageClient(None, cfg)
    prompt = ic.build_prompt(
        "сделай теплее",
        [("floor", "oak wood"), ("left wall", "plaster")],
    )
    assert "Reference 1 is the mold" in prompt
    assert "Reference 2 is the oak wood texture: apply it to the floor" in prompt
    assert "Reference 3 is the plaster texture" in prompt
    assert "geometry" in prompt.lower()


def test_chat_queue_serializes():
    async def go():
        q = ChatQueue()
        order = []

        async def worker(tag, delay):
            async with q.slot(1):
                order.append(f"{tag}-in")
                await asyncio.sleep(delay)
                order.append(f"{tag}-out")

        t1 = asyncio.create_task(worker("a", 0.05))
        await asyncio.sleep(0.01)
        assert q.busy(1)
        t2 = asyncio.create_task(worker("b", 0.0))
        await asyncio.gather(t1, t2)
        assert order == ["a-in", "a-out", "b-in", "b-out"]
        # another chat is independent
        assert not q.busy(2)

    asyncio.run(go())


def test_queue_cancel():
    async def go():
        q = ChatQueue()

        async def worker():
            async with q.slot(5):
                await asyncio.sleep(10)

        t = asyncio.create_task(worker())
        await asyncio.sleep(0.01)
        q.track(5, t)
        assert q.cancel(5)
        try:
            await t
        except asyncio.CancelledError:
            pass
        assert not q.cancel(5)  # nothing to cancel after

    asyncio.run(go())
