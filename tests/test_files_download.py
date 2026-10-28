"""FileStorage.download must pass the file *path string* to bot.download_file()
and infer mime from that path.

Regressions covered:
1. passing the whole File object put its repr into the Telegram URL
   (.../file/bot<token>/file_id='...' file_path='photos/file_5.jpg') -> 404;
2. aiogram's File has no mime_type attribute (fields: file_id, file_unique_id,
   file_size, file_path) -> AttributeError on tg_file.mime_type.
"""
import io

from bot.config import Config
from bot.services.files import FileStorage


class FakeFile:
    """Mirrors aiogram.types.File: no mime_type field."""

    def __init__(self, file_path: str | None, file_size: int = 100) -> None:
        self.file_path = file_path
        self.file_size = file_size

    def __str__(self) -> str:  # mimic the pydantic repr that leaked into the URL
        return (
            f"file_id='AgAC' file_unique_id='AQAD' file_size={self.file_size} "
            f"file_path='{self.file_path}'"
        )


class FakeBot:
    def __init__(self, file: FakeFile) -> None:
        self.file = file
        self.downloaded_with: object | None = None

    async def get_file(self, file_id: str) -> FakeFile:
        return self.file

    async def download_file(self, file_path: str) -> io.BytesIO:
        assert isinstance(file_path, str), f"expected path str, got {type(file_path)}"
        self.downloaded_with = file_path
        return io.BytesIO(b"image-bytes")


def _storage(tmp_path) -> FileStorage:
    cfg = Config(bot_token="t", openrouter_api_key="k", data_dir=str(tmp_path))
    return FileStorage(db=None, cfg=cfg)  # type: ignore[arg-type]


async def test_download_passes_file_path_string(tmp_path):
    bot = FakeBot(FakeFile("photos/file_5.jpg"))
    data, mime = await _storage(tmp_path).download(bot, "AgAC")  # type: ignore[arg-type]
    assert data == b"image-bytes"
    assert mime == "image/jpeg"  # guessed from the path, no mime_type on File
    assert bot.downloaded_with == "photos/file_5.jpg"


async def test_download_voice_path_mime(tmp_path):
    bot = FakeBot(FakeFile("voice/file_1.ogg"))
    _data, mime = await _storage(tmp_path).download(bot, "AgAC")  # type: ignore[arg-type]
    assert mime == "audio/ogg"


async def test_download_unknown_extension_falls_back(tmp_path):
    bot = FakeBot(FakeFile("documents/file_1"))
    _data, mime = await _storage(tmp_path).download(bot, "AgAC")  # type: ignore[arg-type]
    assert mime == "application/octet-stream"


async def test_download_refuses_missing_path(tmp_path):
    bot = FakeBot(FakeFile(None))
    got = await _storage(tmp_path).download(bot, "AgAC")  # type: ignore[arg-type]
    assert got is None
    assert bot.downloaded_with is None


async def test_download_refuses_too_big(tmp_path):
    from bot.services.files import MAX_DOWNLOAD_BYTES

    bot = FakeBot(FakeFile("photos/big.jpg", file_size=MAX_DOWNLOAD_BYTES + 1))
    got = await _storage(tmp_path).download(bot, "AgAC")  # type: ignore[arg-type]
    assert got is None
    assert bot.downloaded_with is None
