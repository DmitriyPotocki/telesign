"""One-stage flow + base gallery: brief-from-caption, pending choice store,
editbase handler and gallery composition."""
from types import SimpleNamespace

import pytest

import bot.handlers.messages as messages_mod
from bot.config import Config
from bot.services.pipeline import (
    _PENDING_BASE_CHOICES,
    PENDING_CHOICE_TTL,
    Pipeline,
    pop_base_choice,
    stash_base_choice,
)


# --- brief from caption (one-stage) --------------------------------------------
def test_brief_from_caption():
    from bot.handlers.messages import _brief_from_caption

    assert _brief_from_caption(None) == ""
    assert _brief_from_caption("молд: гостиная") == "гостиная"  # role prefix stripped
    assert _brief_from_caption("текстура:") == ""  # role only, nothing to run
    assert _brief_from_caption("сделай скандинавский стиль") == "сделай скандинавский стиль"


# --- pending base choices -------------------------------------------------------
def test_stash_and_pop_roundtrip():
    token = stash_base_choice("сделай теплее", {1: 55, 2: 66})
    entry = pop_base_choice(token)
    assert entry is not None
    assert entry["brief"] == "сделай теплее"
    assert entry["mapping"] == {1: 55, 2: 66}
    assert pop_base_choice(token) is None  # one-shot


def test_stash_purges_expired_entries():
    old = stash_base_choice("старый", {1: 1})
    _PENDING_BASE_CHOICES[old]["ts"] -= PENDING_CHOICE_TTL + 1
    fresh = stash_base_choice("новый", {1: 2})
    assert old not in _PENDING_BASE_CHOICES
    assert pop_base_choice(fresh) is not None


def test_pop_expired_entry_returns_none():
    token = stash_base_choice("б", {1: 2})
    _PENDING_BASE_CHOICES[token]["ts"] -= PENDING_CHOICE_TTL + 1
    assert pop_base_choice(token) is None
    assert token not in _PENDING_BASE_CHOICES


# --- editbase handler ------------------------------------------------------------
class _Cb:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=7)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=42))
        self.answers: list[tuple] = []

    async def answer(self, *args, **kwargs) -> None:
        self.answers.append((args, kwargs))


async def test_editbase_runs_pipeline_with_override(monkeypatch):
    token = stash_base_choice("сделай теплее", {1: 55})
    recorded: dict = {}

    async def fake_run(ctx, bot, chat_id, user_id, brief, **kw):
        recorded.update(chat_id=chat_id, user_id=user_id, brief=brief, **kw)

    monkeypatch.setattr(messages_mod, "_run_pipeline", fake_run)
    cb = _Cb(f"editbase:{token}:1")

    await messages_mod.on_edit_base_choice(cb, None, None)  # type: ignore[arg-type]

    assert recorded["brief"] == "сделай теплее"
    assert recorded["base_override"] == 55
    assert recorded["chat_id"] == 42 and recorded["user_id"] == 7
    assert cb.answers


async def test_editbase_unknown_token_alerts(monkeypatch):
    calls: list = []

    async def fake_run(*args, **kwargs):
        calls.append(1)

    monkeypatch.setattr(messages_mod, "_run_pipeline", fake_run)
    cb = _Cb("editbase:ffffffff:1")

    await messages_mod.on_edit_base_choice(cb, None, None)  # type: ignore[arg-type]

    assert calls == []
    assert cb.answers and cb.answers[0][1].get("show_alert") is True


async def test_editbase_bad_number_alerts(monkeypatch):
    token = stash_base_choice("б", {1: 55})
    calls: list = []

    async def fake_run(*args, **kwargs):
        calls.append(1)

    monkeypatch.setattr(messages_mod, "_run_pipeline", fake_run)
    cb = _Cb(f"editbase:{token}:99")

    await messages_mod.on_edit_base_choice(cb, None, None)  # type: ignore[arg-type]

    assert calls == []
    assert cb.answers
    # a wrong tap must NOT consume the pending brief
    assert pop_base_choice(token)["brief"] == "б"


# --- caption sanitizer (never persist the prompt as a name) ----------------------
def test_clean_caption_never_keeps_long_prompts():
    from bot.handlers.messages import _clean_caption

    assert _clean_caption(None) is None
    assert _clean_caption("молд: гостиная") == "гостиная"  # role label only
    assert _clean_caption("дуб тёмный") == "дуб тёмный"  # short hint kept
    long_prompt = "сделай скандинавский стиль, пол светлый дуб, стены белила, 4 варианта"
    assert _clean_caption(long_prompt) is None  # the brief is not a file name


# --- gallery composition: generated variants, not uploads -------------------------
def _cand(file_id: int, path, *, winner: bool = False, total: float = 0.0) -> dict:
    import json as _json

    return {
        "file_id": file_id,
        "path": str(path),
        "is_winner": int(winner),
        "score_json": _json.dumps({"total": total}),
    }


def _file_row(id_: int, path, kind: str, caption=None, analysis=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_, path=str(path), kind=kind, caption=caption, analysis=analysis
    )


class _GalleryDB:
    def __init__(self, cands, uploads) -> None:
        self._cands = cands
        self._uploads = uploads

    async def get_active_project(self, user_id):
        return 1

    async def get_active_step(self, project_id):
        return SimpleNamespace(id=5, winner_file_id=9)

    async def list_candidates(self, step_id):
        return list(self._cands)

    async def list_files(self, user_id, limit=50):
        return list(self._uploads)


def _pipeline(db) -> Pipeline:
    cfg = Config(bot_token="t", openrouter_api_key="k")
    return Pipeline(db=db, cfg=cfg, client=None, storage=None, streamer=None)  # type: ignore[arg-type]


async def test_gallery_shows_generated_variants_not_uploads(tmp_path):
    win = tmp_path / "w.png"
    win.write_bytes(b"x")
    alt1 = tmp_path / "a1.png"
    alt1.write_bytes(b"x")
    alt2 = tmp_path / "a2.png"
    alt2.write_bytes(b"x")
    mold = tmp_path / "m.png"
    mold.write_bytes(b"x")   # upload: must NOT be listed

    db = _GalleryDB(
        cands=[
            _cand(21, alt1, total=7.5),
            _cand(9, win, winner=True, total=8.9),
            _cand(22, alt2, total=6.1),
        ],
        uploads=[_file_row(1, mold, "mold", "гостиная")],
    )

    entries = await _pipeline(db)._base_gallery(1)

    assert [e["file_id"] for e in entries] == [9, 21, 22]  # winner first, then by score
    assert entries[0]["label"] == "текущий результат"
    assert entries[1]["label"] == "альтернатива"
    assert entries[2]["label"] == "альтернатива"
    assert all(e["file_id"] != 1 for e in entries), "uploads are not alternatives"


async def test_gallery_fallback_uses_model_names_for_textures(tmp_path):
    tex = tmp_path / "t.png"
    tex.write_bytes(b"z")
    mold = tmp_path / "m.png"
    mold.write_bytes(b"y")

    db = _GalleryDB(
        cands=[],  # nothing generated yet
        uploads=[
            _file_row(5, tex, "texture", caption="СДЕЛАЙ СКАНДИНАВСКИЙ ПОЛ И ЕЩЁ КУЧУ ВСЕГО",
                      analysis="дуб тёмный, направленный рисунок"),
            _file_row(1, mold, "mold", caption="гостиная"),
        ],
    )

    entries = await _pipeline(db)._base_gallery(1)

    assert [e["file_id"] for e in entries] == [5, 1]
    # texture named by the MODEL (analysis), never by the user's prompt
    assert "дуб тёмный" in entries[0]["label"]
    assert "СКАНДИНАВСКИЙ" not in entries[0]["label"]
    # mold may keep a short user caption
    assert "гостиная" in entries[1]["label"]


async def test_gallery_skips_missing_files(tmp_path):
    gone = tmp_path / "gone.png"  # never created
    db = _GalleryDB(cands=[_cand(7, gone, winner=True)], uploads=[])
    assert await _pipeline(db)._base_gallery(1) == []


async def test_base_gallery_empty_without_files():
    class DB:
        async def get_active_project(self, user_id):
            return 1

        async def get_active_step(self, project_id):
            return None

        async def list_candidates(self, step_id):
            return []

        async def list_files(self, user_id, limit=50):
            return []

    cfg = Config(bot_token="t", openrouter_api_key="k")
    pipeline = Pipeline(db=DB(), cfg=cfg, client=None, storage=None, streamer=None)  # type: ignore[arg-type]
    assert await pipeline._base_gallery(1) == []


async def test_send_base_gallery_documents_and_buttons(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    entries = [
        {"file_id": 1, "label": "текущий результат", "path": str(img)},
        {"file_id": 2, "label": "альтернатива", "path": str(img)},
        {"file_id": 3, "label": "альтернатива", "path": str(img)},
    ]

    class FakeBot:
        def __init__(self) -> None:
            self.docs: list[dict] = []
            self.messages: list[dict] = []

        async def send_document(self, chat_id, document, caption=None):
            self.docs.append({"chat_id": chat_id, "caption": caption})

        async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
            self.messages.append({"text": text, "reply_markup": reply_markup})
            return SimpleNamespace(message_id=1)

    cfg = Config(bot_token="t", openrouter_api_key="k")
    pipeline = Pipeline(db=None, cfg=cfg, client=None, storage=None, streamer=None)  # type: ignore[arg-type]
    bot = FakeBot()

    await pipeline._send_base_gallery(42, bot, "правь пол", entries)

    assert [d["caption"] for d in bot.docs] == [
        "№1 — текущий результат",
        "№2 — альтернатива",
        "№3 — альтернатива",
    ]
    kb = bot.messages[0]["reply_markup"]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert all(d.startswith("editbase:") for d in datas)
    assert len(datas) == 3
    token = datas[0].split(":")[1]
    assert pop_base_choice(token)["brief"] == "правь пол"


@pytest.mark.parametrize("data", ["editbase:bad", "editbase:x:y:z"])
async def test_editbase_malformed_data_ignored(data):
    cb = _Cb(data)
    await messages_mod.on_edit_base_choice(cb, None, None)  # type: ignore[arg-type]
    assert cb.answers  # acknowledged, nothing crashed
