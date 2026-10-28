"""_run_job must feed build_prompt with (surface, texture) pairs, not strings.

Regression: pipeline passed a list[str] like "пол <- дуб" while build_prompt
unpacks two values -> ValueError: too many values to unpack (expected 2).
"""
import pathlib

import pytest

from bot.config import Config
from bot.llm.schemas import Job, Material
from bot.services.pipeline import Pipeline

PNG = pathlib.Path(__file__).parent / "fixtures" / "mold.png"


class StopHere(Exception):
    """Raised right after build_prompt to stop before generation/judge."""


class FakeStorage:
    def __init__(self, image: bytes) -> None:
        self.image = image

    async def read_bytes(self, user_id: int, file_id: int) -> bytes | None:
        return self.image


class FakeStreamer:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    async def status(self, text: str) -> None:
        self.statuses.append(text)

    async def finalize(self, text: str) -> None:
        self.statuses.append(text)


class CapturingImageClient:
    """build_prompt delegates to the real implementation (contract check)."""

    def __init__(self, cfg: Config) -> None:
        from bot.imaging.generate import ImageClient

        self._real = ImageClient(None, cfg)
        self.captured = None

    def build_prompt(self, changes: str, materials):
        self.captured = materials
        return self._real.build_prompt(changes, materials)  # unpacks pairs -> ValueError if not

    async def generate_variants(self, *args, **kwargs):
        raise StopHere


async def test_run_job_passes_pairs_to_build_prompt():
    cfg = Config(bot_token="t", openrouter_api_key="k")
    image = PNG.read_bytes()
    pipeline = Pipeline(
        db=None, cfg=cfg, client=None, storage=FakeStorage(image),  # type: ignore[arg-type]
        streamer=FakeStreamer(),  # type: ignore[arg-type]
    )
    ic = CapturingImageClient(cfg)
    pipeline.image_client = ic  # type: ignore[assignment]

    job = Job(
        base_file_id=1,
        n_variants=2,
        prompt="apply oak to the floor",
        materials=[
            Material(surface="пол", texture_file_id=15, texture_name="дуб"),
            Material(surface="стена", texture_file_id=16),  # no name -> file id fallback
        ],
    )

    with pytest.raises(StopHere):
        await pipeline._run_job(
            bot=None, user_id=1, chat_id=1, step=None, job=job,  # type: ignore[arg-type]
            materials=job.materials, base_file_id=1,
        )

    assert ic.captured is not None
    assert all(isinstance(p, tuple) and len(p) == 2 for p in ic.captured)
    assert ("пол", "дуб") in ic.captured
    assert ("стена", "16") in ic.captured  # str fallback when texture_name is None
