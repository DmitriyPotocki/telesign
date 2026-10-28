"""Application context shared by all handlers."""
from __future__ import annotations

import dataclasses

from ..config import Config
from ..db.repo import Database
from ..llm.openrouter import OpenRouter
from ..services.files import FileStorage
from ..services.pipeline import Pipeline
from ..services.userqueue import ChatQueue
from ..stt.base import Transcriber
from ..telegram.draft_streamer import DraftStreamer


@dataclasses.dataclass
class AppContext:
    cfg: Config
    db: Database
    client: OpenRouter
    storage: FileStorage
    transcriber: Transcriber
    queue: ChatQueue
    pipeline_factory: object  # Callable[[DraftStreamer], Pipeline]

    def make_pipeline(self, streamer: DraftStreamer) -> Pipeline:
        return self.pipeline_factory(streamer)  # type: ignore[operator]
