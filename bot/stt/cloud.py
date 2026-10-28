"""Cloud STT via OpenRouter /audio/transcriptions.

Default model: mistralai/voxtral-small-transcribe-3-0 (Voxtral Small Transcribe 3).
Telegram voice = OGG/Opus. The STT API supports `format: ogg`, but Voxtral's own
example shows wav — so if the provider rejects ogg (400), we fall back to
converting the file to wav via ffmpeg and retry once.
"""
from __future__ import annotations

import logging
import pathlib

from ..config import Config
from ..llm.openrouter import OpenRouter, OpenRouterError
from ..stt.whisper_local import convert_ogg_to_wav
from .base import Transcriber

logger = logging.getLogger(__name__)


class CloudTranscriber(Transcriber):
    def __init__(self, cfg: Config, client: OpenRouter | None = None) -> None:
        self.cfg = cfg
        self.client = client

    def _get_client(self) -> OpenRouter:
        if self.client is None:
            self.client = OpenRouter(self.cfg.openrouter_api_key, self.cfg.openrouter_base_url)
        return self.client

    async def warmup(self) -> None:
        pass

    async def transcribe(self, audio_path: pathlib.Path, mime: str) -> str:
        client = self._get_client()
        is_ogg = "ogg" in (mime or "") or audio_path.suffix.lower() == ".ogg"
        data = audio_path.read_bytes()
        fmt = "ogg" if is_ogg else "wav"
        try:
            return await client.transcribe(data, fmt, model=self.cfg.stt_model)
        except OpenRouterError as e:
            if not (is_ogg and "400" in str(e)):
                raise
            # provider rejected ogg -> convert to wav via ffmpeg and retry
            logger.info("cloud STT rejected ogg, converting to wav: %s", e)
            wav = audio_path.with_suffix(".wav")
            convert_ogg_to_wav(audio_path, wav)
            return await client.transcribe(wav.read_bytes(), "wav", model=self.cfg.stt_model)
