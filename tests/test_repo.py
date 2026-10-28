import asyncio

import pytest

from bot.db.repo import Database


@pytest.fixture
def db(tmp_path):
    async def go():
        d = Database(tmp_path / "test.sqlite3")
        await d.connect()
        await d.migrate()
        await d.ensure_user(1)
        return d

    db = asyncio.run(go())
    yield db
    asyncio.run(db.close())


def run(coro):
    return asyncio.run(coro)


def test_instruction_crud(db):
    async def go():
        assert await db.count_instructions(1) == 0
        await db.add_instruction(1, "Всегда сохраняй пол из молда", "text", None)
        await db.add_instruction(1, "Скандинавский стиль", "voice", "tgfile123")
        rows = await db.list_instructions(1)
        assert len(rows) == 2
        assert rows[1]["source"] == "voice"
        assert rows[1]["file_id"] == "tgfile123"
        ok = await db.delete_instruction(1, rows[0]["id"])
        assert ok
        # cannot delete other user's instruction
        await db.ensure_user(2)
        assert not await db.delete_instruction(2, rows[1]["id"])
        assert await db.count_instructions(1) == 1

    run(go())


def test_file_dedup_by_sha(db):
    async def go():
        await db.add_file(1, "mold", "/tmp/a.png", "sha1", "tg1", "image/png", "молд: гостиная")
        existing = await db.find_file_by_sha(1, "sha1")
        assert existing is not None
        assert existing.kind == "mold"
        # another user does not see it
        assert await db.find_file_by_sha(2, "sha1") is None

    run(go())


def test_file_ownership_isolation(db):
    async def go():
        fid = await db.add_file(1, "texture", "/tmp/t.png", "shaT", None, None, None)
        assert await db.get_file(2, fid) is None
        assert await db.get_file(1, fid) is not None
        # deleting by other user fails
        assert await db.delete_file(2, fid) is None
        assert await db.get_file(1, fid) is not None

    run(go())


def test_steps_and_rollback(db):
    async def go():
        pid = await db.get_active_project(1)
        s1 = await db.add_step(pid, "пол дуб", None, 10, None)
        w1 = await db.add_file(1, "result", "/tmp/r1.png", "r1", None, None, None)
        await db.finish_step(s1.id, w1)
        s2 = await db.add_step(pid, "поменяй диван", None, w1, s1.id)
        w2 = await db.add_file(1, "result", "/tmp/r2.png", "r2", None, None, None)
        await db.finish_step(s2.id, w2)

        active = await db.get_active_step(pid)
        assert active.id == s2.id
        # rollback to step 1
        await db.set_active_step(pid, s1.id)
        active = await db.get_active_step(pid)
        assert active.id == s1.id
        assert active.winner_file_id == w1
        # chain intact: parent links preserved
        s2row = await db.get_step(s2.id)
        assert s2row.parent_step_id == s1.id
        steps = await db.list_steps(pid)
        assert [s.id for s in steps] == [s1.id, s2.id]

    run(go())


def test_candidates(db):
    async def go():
        pid = await db.get_active_project(1)
        s = await db.add_step(pid, "x", None, 10, None)
        f1 = await db.add_file(1, "result", "/tmp/c1.png", "c1", None, None, None)
        f2 = await db.add_file(1, "result", "/tmp/c2.png", "c2", None, None, None)
        await db.add_candidate(s.id, f1, '{"total": 7.1}', is_winner=True)
        await db.add_candidate(s.id, f2, '{"total": 6.2}', is_winner=False)
        cands = await db.list_candidates(s.id)
        assert len(cands) == 2
        winners = [c for c in cands if c["is_winner"]]
        assert len(winners) == 1 and winners[0]["file_id"] == f1

    run(go())


def test_daily_image_limit_counting(db):
    async def go():
        assert await db.images_today(1) == 0
        await db.log_usage(1, "image", "krea/k3-edit", images=4)
        await db.log_usage(1, "chat", "anthropic/claude-haiku-5.5", tokens_in=10, tokens_out=5)
        await db.log_usage(1, "image", "krea/k3-edit", images=2)
        assert await db.images_today(1) == 6

    run(go())


def test_new_project_keeps_instructions(db):
    async def go():
        await db.add_instruction(1, "instr", "text", None)
        pid = await db.create_project(1)
        assert await db.get_active_project(1) == pid  # latest
        assert await db.count_instructions(1) == 1

    run(go())
