"""Per-chat request queue: one active pipeline run per chat, the rest wait.
Supports cooperative cancellation via /cancel (active_task registry)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ChatQueue:
    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    def _lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    def busy(self, chat_id: int) -> bool:
        return self._lock(chat_id).locked()

    @asynccontextmanager
    async def slot(self, chat_id: int, notify_wait=None) -> AsyncIterator[None]:
        lock = self._lock(chat_id)
        waiting = lock.locked()
        if waiting and notify_wait:
            try:
                await notify_wait()
            except Exception:
                pass
        async with lock:
            yield

    def track(self, chat_id: int, task: asyncio.Task) -> None:
        self._tasks[chat_id] = task

    def untrack(self, chat_id: int, task: asyncio.Task) -> None:
        if self._tasks.get(chat_id) is task:
            self._tasks.pop(chat_id, None)

    def cancel(self, chat_id: int) -> bool:
        task = self._tasks.get(chat_id)
        if task and not task.done():
            task.cancel()
            return True
        return False
