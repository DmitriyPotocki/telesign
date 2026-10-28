"""Viewer phase: streamed 'viewing image' blocks, per-file analysis cache,
schema v2 migration and the orchestrator receiving what the viewer saw."""
import pathlib
from types import SimpleNamespace

from bot.config import Config
from bot.db.repo import SCHEMA_PATH, Database
from bot.llm.orchestrator import build_orchestrator_messages
from bot.services.pipeline import Pipeline

PNG = pathlib.Path(__file__).parent / "fixtures" / "mold.png"


# --- fakes --------------------------------------------------------------------
class FakeClient:
    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def chat_stream(self, model, messages, **kwargs):
        self.calls.append(messages)
        user_content = messages[-1]["content"]
        if isinstance(user_content, list):  # vision call (viewing)
            yield "Комната с панорамным окном, пол — бетон."
        else:  # text-only (synthesis)
            yield "Пол → дуб, стены → белила."


class FakeStorage:
    async def read_bytes(self, user_id, file_id):
        return PNG.read_bytes()


class FakeDB:
    def __init__(self) -> None:
        self.saved: dict[int, str] = {}

    async def set_file_analysis(self, user_id, file_id, analysis):
        self.saved[file_id] = analysis


class FakeStreamer:
    async def status(self, text): ...
    async def finalize(self, text): ...


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self._mid = 0

    async def send_message(self, chat_id, text, parse_mode=None):
        self._mid += 1
        self.sent.append(text)
        return SimpleNamespace(message_id=self._mid)

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None):
        self.sent.append(text)


def make_pipeline() -> tuple[Pipeline, FakeClient, FakeDB]:
    cfg = Config(bot_token="t", openrouter_api_key="k")
    client = FakeClient()
    db = FakeDB()
    pipeline = Pipeline(
        db=db, cfg=cfg, client=client, storage=FakeStorage(),  # type: ignore[arg-type]
        streamer=FakeStreamer(),  # type: ignore[arg-type]
    )
    return pipeline, client, db


# --- tests --------------------------------------------------------------------
async def test_uncached_files_are_viewed_streamed_and_cached():
    pipeline, client, db = make_pipeline()
    bot = FakeBot()
    files = [
        {"id": 5, "kind": "mold", "caption": "гостиная", "analysis": None},
        {"id": 6, "kind": "texture", "caption": "дуб", "analysis": None},
    ]

    take = await pipeline._viewer_phase(1, 42, bot, files, brief="сделай скандинавский")

    # two vision calls (one per image) + one synthesis
    assert len(client.calls) == 3
    vision = client.calls[0]
    user_content = vision[-1]["content"]
    assert isinstance(user_content, list)
    assert any(c.get("type") == "image_url" for c in user_content)  # sees pixels

    # analysis cached in DB and fed back into the same dicts
    assert db.saved[5] and db.saved[6]
    assert files[0]["analysis"] == db.saved[5]

    # numbered tool-call style blocks; the model names images itself —
    # user captions (often the whole prompt) must NOT appear in headers
    headers = [m for m in bot.sent if "смотрю изображение" in m]
    assert any("Шаг #1 · смотрю изображение: молд #5" in m for m in headers)
    assert any("Шаг #2 · смотрю изображение: текстура #6" in m for m in headers)
    assert not any("гостиная" in m for m in bot.sent)   # caption dropped from header
    assert not any("— дуб" in m for m in bot.sent)
    assert any("Шаг #3 · анализ" in m for m in bot.sent)  # synthesis step is shown
    assert all(m.startswith("<blockquote>") for m in bot.sent)
    assert take.startswith("Пол → дуб")


async def test_cached_files_are_not_re_viewed_but_feed_synthesis():
    pipeline, client, _db = make_pipeline()
    bot = FakeBot()
    files = [
        {"id": 5, "kind": "mold", "caption": None, "analysis": "бетон, панорама"},
        {"id": 6, "kind": "texture", "caption": None, "analysis": "дуб, направленный рисунок"},
    ]

    take = await pipeline._viewer_phase(1, 42, bot, files, brief="тёплый свет")

    assert len(client.calls) == 1  # synthesis only — no vision calls
    assert not any("смотрю изображение" in m for m in bot.sent)  # no viewing blocks
    assert any("анализ" in m for m in bot.sent)
    synth_text = client.calls[0][-1]["content"]
    assert "бетон, панорама" in synth_text  # cached notes still reach planning
    assert take


async def test_no_media_shortcut():
    pipeline, client, _db = make_pipeline()
    bot = FakeBot()

    take = await pipeline._viewer_phase(1, 42, bot, [], brief="привет")
    assert take == ""
    assert client.calls == []
    assert bot.sent == []

    take2 = await pipeline._viewer_phase(
        1, 42, bot, [{"id": 1, "kind": "voice", "caption": None, "analysis": None}], brief="х"
    )
    assert take2 == ""
    assert client.calls == []


async def test_max_view_files_cap():
    pipeline, client, _db = make_pipeline()
    bot = FakeBot()
    from bot.services.pipeline import MAX_VIEW_FILES

    files = [
        {"id": i, "kind": "texture", "caption": None, "analysis": None}
        for i in range(1, MAX_VIEW_FILES + 4)
    ]
    await pipeline._viewer_phase(1, 42, bot, files, brief="бриф")
    vision_calls = [
        c for c in client.calls if isinstance(c[-1]["content"], list)
    ]
    assert len(vision_calls) == MAX_VIEW_FILES


# --- schema v2 ----------------------------------------------------------------
async def test_migration_v2_adds_analysis_column(tmp_path):
    db = Database(tmp_path / "old.db")
    await db.connect()
    # build a v1 database: schema without the analysis column
    old_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    line = "    analysis TEXT,  -- cached vision description (viewer phase), v2\n"
    assert line in old_sql, "schema.sql must declare analysis (comment may change)"
    await db.execute_script(old_sql.replace(line, ""))
    await db.execute("INSERT INTO meta(key, value) VALUES('schema_version', '1')")
    await db.conn.commit()

    await db.migrate()

    cur = await db.conn.execute("PRAGMA table_info(files)")
    cols = [r[1] for r in await cur.fetchall()]
    assert "analysis" in cols

    # FileRow roundtrip + setter
    await db.ensure_user(7)
    fid = await db.add_file(user_id=7, kind="mold", path="/tmp/x.png", sha256="abc")
    row = await db.get_file(7, fid)
    assert row is not None and row.analysis is None
    await db.set_file_analysis(7, fid, "стены: бетон, окно справа")
    row2 = await db.get_file(7, fid)
    assert row2.analysis == "стены: бетон, окно справа"

    await db.migrate()  # idempotent
    await db.close()


async def test_fresh_database_is_v2(tmp_path):
    db = Database(tmp_path / "new.db")
    await db.connect()
    await db.migrate()
    cur = await db.conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = await cur.fetchone()
    assert int(row[0]) == 2
    cur2 = await db.conn.execute("PRAGMA table_info(files)")
    cols = [r[1] for r in await cur2.fetchall()]
    assert "analysis" in cols
    await db.close()


# --- orchestrator context -----------------------------------------------------
def test_analysis_and_synthesis_reach_orchestrator():
    msgs = build_orchestrator_messages(
        "сделай светлее",
        [{"id": 5, "kind": "mold", "caption": None, "analysis": "бетон, панорамное окно"}],
        instructions=[],
        history=[],
        default_variants=4,
        viewer_notes=["Пол → дуб, стены → белила."],
    )
    content = msgs[1]["content"]
    assert "sees: бетон, панорамное окно" in content
    assert "Viewer's synthesis" in content
    assert "Пол → дуб" in content


def test_files_without_analysis_stay_clean():
    msgs = build_orchestrator_messages(
        "б",
        [{"id": 5, "kind": "mold", "caption": None, "analysis": None}],
        instructions=[],
        history=[],
        default_variants=4,
    )
    assert "sees:" not in msgs[1]["content"]
