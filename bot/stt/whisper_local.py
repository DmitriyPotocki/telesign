"""Local STT: faster-whisper small / CPU / int8, OGG/Opus via ffmpeg -> wav."""
from __future__ import annotations

import asyncio
import logging
import pathlib
import subprocess

from ..config import Config
from .base import Transcriber

logger = logging.getLogger(__name__)


def convert_ogg_to_wav(src: pathlib.Path, dst: pathlib.Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
            "-ar", "16000", "-ac", "1", "-f", "wav", str(dst),
        ],
        check=True,
        capture_output=True,
    )


class WhisperLocal(Transcriber):
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._model = None

    async def warmup(self) -> None:
        if self._model is None:
            await asyncio.get_running_loop().run_in_executor(
                None, self._load_model, self.cfg.whisper_model_size
            )

    def _load_model(self, size: str) -> None:
        from faster_whisper import WhisperModel  # lazy import: heavy

        logger.info("loading faster-whisper model %s (int8, cpu)…", size)
        self._model = WhisperModel(size, device="cpu", compute_type="int8")

    async def transcribe(self, audio_path: pathlib.Path, mime: str) -> str:
        await self.warmup()
        wav = audio_path.with_suffix(".wav")

        def _run() -> str:
            if audio_path.suffix.lower() != ".wav":
                convert_ogg_to_wav(audio_path, wav)
            segments, _info = self._model.transcribe(str(wav if wav.exists() else audio_path))
            return " ".join(s.text.strip() for s in segments).strip()

        return await asyncio.get_running_loop().run_in_executor(None, _run)
