"""File storage: DATA_DIR/<user_id>/<kind>/<sha256>.<ext>, dedupe by sha256."""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import pathlib
import re

from aiogram import Bot

from ..config import Config
from ..db.repo import Database

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # refuse files > 20 MB

_KIND_BY_CAPTION = {
    "молд": "mold",
    "mold": "mold",
    "болванка": "mold",
    "текстура": "texture",
    "texture": "texture",
    "материал": "texture",
    "референс": "reference",
    "reference": "reference",
    "стиль": "reference",
}

_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
}


def role_from_caption(caption: str | None) -> tuple[str | None, str | None]:
    """Parse caption like 'текстура: дуб' -> (kind, label)."""
    if not caption:
        return None, None
    head = caption.split(":", 1)[0].strip().lower()
    for word, kind in _KIND_BY_CAPTION.items():
        if head == word or head.startswith(word + " ") or head == word + "ы":
            label = caption.split(":", 1)[1].strip() if ":" in caption else None
            return kind, label
    return None, None


class FileStorage:
    def __init__(self, db: Database, cfg: Config) -> None:
        self.db = db
        self.cfg = cfg

    def _user_dir(self, user_id: int, kind: str) -> pathlib.Path:
        d = pathlib.Path(self.cfg.data_dir) / str(user_id) / kind
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def download(self, bot: Bot, file_id: str) -> tuple[bytes, str] | None:
        """Download from Telegram; refuse too big. Returns (bytes, mime) or None."""
        tg_file = await bot.get_file(file_id)
        size = tg_file.file_size or 0
        if size > MAX_DOWNLOAD_BYTES:
            logger.info("file %s too big (%s bytes)", file_id, size)
            return None
        # download_file() expects the file *path* string, not the File object
        # (passing the object puts its repr into the URL -> 404)
        if not tg_file.file_path:
            logger.warning("telegram returned no file_path for %s", file_id)
            return None
        buf = await bot.download_file(tg_file.file_path)
        data = buf.read() if hasattr(buf, "read") else buf  # type: ignore[attr-defined]
        # aiogram's File has no mime_type (fields: file_id/unique_id/size/path) —
        # infer it from the Telegram file path, e.g. "photos/file_5.jpg"
        mime = mimetypes.guess_type(tg_file.file_path)[0] or "application/octet-stream"
        return data, mime

    async def save_image(
        self,
        user_id: int,
        bot: Bot,
        file_id: str,
        caption: str | None,
        default_kind: str = "mold",
        use_llm_role=None,
    ) -> tuple[int, str] | None:
        """Save photo/file image. Returns (files.id, kind) or None if skipped."""
        got = await self.download(bot, file_id)
        if got is None:
            return None
        data, mime = got

        kind, _label = role_from_caption(caption)
        if kind is None:
            kind = default_kind
        # store the original bytes untouched; model inputs are resized at use time
        return await self.store_bytes(user_id, data, mime, kind, caption, tg_file_id=file_id)

    async def store_bytes(
        self,
        user_id: int,
        data: bytes,
        mime: str,
        kind: str,
        caption: str | None,
        tg_file_id: str | None = None,
    ) -> tuple[int, str]:
        sha = hashlib.sha256(data).hexdigest()
        # dedupe
        existing = await self.db.find_file_by_sha(user_id, sha)
        if existing:
            if caption and not existing.caption:
                await self.db.set_file_caption(user_id, existing.id, caption)
            return existing.id, existing.kind

        ext = _MIME_EXT.get(mime, "bin")
        d = self._user_dir(user_id, kind)
        path = d / f"{sha}.{ext}"
        path.write_bytes(data)
        file_id = await self.db.add_file(
            user_id=user_id,
            kind=kind,
            path=str(path),
            sha256=sha,
            tg_file_id=tg_file_id,
            mime=mime,
            caption=caption,
        )
        return file_id, kind

    async def read_bytes(self, user_id: int, file_id: int) -> bytes | None:
        row = await self.db.get_file(user_id, file_id)
        if not row:
            return None
        p = pathlib.Path(row.path)
        if not p.exists():
            logger.warning("file record %s points to missing path %s", file_id, row.path)
            return None
        return p.read_bytes()

    def safe_path(self, user_id: int, name: str) -> pathlib.Path:
        """Guard against path traversal when exporting file names."""
        base = pathlib.Path(self.cfg.data_dir) / str(user_id)
        candidate = (base / name).resolve()
        if not str(candidate).startswith(str(base.resolve())):
            raise ValueError("path traversal blocked")
        return candidate


def sanitize_text(s: str, max_len: int = 500) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len]
