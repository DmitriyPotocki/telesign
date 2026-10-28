"""Transcriber interface and factory. Haiku accepts no audio -> we need STT."""
from __future__ import annotations

import abc
import pathlib

from ..config import Config


class Transcriber(abc.ABC):
    @abc.abstractmethod
    async def transcribe(self, audio_path: pathlib.Path, mime: str) -> str: ...

    @abc.abstractmethod
    async def warmup(self) -> None: ...


def make_transcriber(cfg: Config) -> Transcriber:
    if cfg.stt_provider == "cloud":
        from .cloud import CloudTranscriber

        return CloudTranscriber(cfg)
    from .whisper_local import WhisperLocal

    return WhisperLocal(cfg)
