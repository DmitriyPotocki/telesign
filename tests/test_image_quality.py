"""Image quality: originals stored untouched, high-res generation inputs,
lossless winner delivery via the "original" button."""
import datetime
import io
import pathlib

from aiogram.types import CallbackQuery, Chat, Message, User

from bot.config import Config
from bot.handlers.messages import router
from bot.imaging.metrics import JPEG_QUALITY, resize_image
from bot.services.files import FileStorage
from bot.services.pipeline import GEN_MAX_SIDE, RESULT_BUTTONS, SCORE_MAX_SIDE

PNG = pathlib.Path(__file__).parent / "fixtures" / "mold.png"


def test_model_reencode_quality_is_high():
    assert JPEG_QUALITY >= 95


def test_generation_inputs_high_res_scoring_low():
    assert GEN_MAX_SIDE > SCORE_MAX_SIDE


def test_resize_caps_max_side_without_upscaling():
    from PIL import Image

    big = io.BytesIO()
    Image.new("RGB", (4000, 2000)).save(big, format="PNG")
    out = Image.open(io.BytesIO(resize_image(big.getvalue(), max_side=500)))
    assert max(out.size) == 500

    small = io.BytesIO()
    Image.new("RGB", (100, 50)).save(small, format="PNG")
    out2 = Image.open(io.BytesIO(resize_image(small.getvalue(), max_side=500)))
    assert out2.size == (100, 50)  # no upscale


# --- save_image keeps original bytes ------------------------------------------
class _File:
    def __init__(self, path: str) -> None:
        self.file_path = path
        self.file_size = PNG.stat().st_size


class _DownloadBot:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def get_file(self, file_id: str) -> _File:
        return _File("photos/x.png")

    async def download_file(self, file_path):
        assert isinstance(file_path, str)  # never the object (URL corruption bug)
        return io.BytesIO(self.data)


class _DB:
    def __init__(self) -> None:
        self.added: dict | None = None

    async def find_file_by_sha(self, user_id, sha):
        return None

    async def add_file(self, **kw):
        self.added = kw
        return 1


async def test_save_image_stores_original_untouched(tmp_path):
    original = PNG.read_bytes()
    cfg = Config(bot_token="t", openrouter_api_key="k", data_dir=str(tmp_path))
    db = _DB()
    storage = FileStorage(db=db, cfg=cfg)  # type: ignore[arg-type]

    file_id, kind = await storage.save_image(
        1, _DownloadBot(original), "AgAC", caption=None
    )

    assert file_id == 1 and kind == "mold"
    stored = pathlib.Path(db.added["path"]).read_bytes()
    assert stored == original, "original bytes must be stored without re-encode"
    assert db.added["mime"] == "image/png"


# --- "original" button: delivery without Telegram recompression ----------------
def make_callback(data: str) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        chat_instance="c",
        from_user=User(id=1, is_bot=False, first_name="D"),
        data=data,
        message=Message(
            message_id=1,
            date=datetime.datetime.now(datetime.UTC),
            chat=Chat(id=42, type="private"),
        ),
    )


def test_result_buttons_set():
    datas = {b.callback_data for row in RESULT_BUTTONS.inline_keyboard for b in row}
    assert datas == {"more", "rollback", "continue_here"}


def test_callback_filter_set():
    f = router.observers["callback_query"].handlers[0].filters[0]
    assert f.callback(make_callback("more")) is True
    assert f.callback(make_callback("original")) is False  # removed: docs are full quality
    assert f.callback(make_callback("show_all")) is False  # removed: variants arrive auto
    assert f.callback(make_callback("no_such_action")) is False

