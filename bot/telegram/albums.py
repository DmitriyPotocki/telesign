"""Album collection: group photos by media_group_id with a short timeout."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from aiogram.types import Message, PhotoSize

logger = logging.getLogger(__name__)
ALBUM_WAIT = 1.0  # seconds to wait for the rest of an album

SINGLE = "single"


@dataclass
class AlbumItem:
    file_id: str
    caption: str | None
    mime: str | None = None


@dataclass
class Album:
    media_group_id: str
    items: list[AlbumItem] = field(default_factory=list)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    first_seen: bool = True


class AlbumCollector:
    def __init__(self) -> None:
        self._groups: dict[str, Album] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}

    async def add(self, message: Message) -> str | Album | None:
        """Returns:
        - SINGLE for a message without media_group_id,
        - the Album object if this is the FIRST message of its group (caller
          should spawn a waiter task),
        - None if the message belongs to a group that already has a waiter.
        """
        mgid = message.media_group_id
        if not mgid:
            return SINGLE
        album = self._groups.get(mgid)
        is_first = album is None
        if album is None:
            album = Album(media_group_id=mgid)
            self._groups[mgid] = album
        photo: PhotoSize | None = message.photo
        if photo:
            album.items.append(AlbumItem(file_id=photo[-1].file_id, caption=message.caption))
        elif message.document:
            album.items.append(
                AlbumItem(
                    file_id=message.document.file_id,
                    caption=message.caption,
                    mime=message.document.mime_type,
                )
            )
        loop = asyncio.get_running_loop()
        if mgid in self._timers:
            self._timers[mgid].cancel()
        self._timers[mgid] = loop.call_later(ALBUM_WAIT, self._flush, mgid)
        return album if is_first else None

    def _flush(self, mgid: str) -> None:
        album = self._groups.pop(mgid, None)
        self._timers.pop(mgid, None)
        if album:
            album.event.set()

    async def wait_album(self, album: Album, timeout: float = ALBUM_WAIT + 5.0) -> Album:
        try:
            await asyncio.wait_for(album.event.wait(), timeout=timeout)
        except TimeoutError:
            self._flush(album.media_group_id)
        return album


def best_photo_file_id(message: Message) -> str | None:
    if message.photo:
        return message.photo[-1].file_id
    if message.document and (message.document.mime_type or "").startswith("image/"):
        return message.document.file_id
    return None
