"""K3 Edit image generation via OpenRouter Image API.

The model takes 1-8 references + prompt and returns ONE image. No masks.
Reference order: #1 always the mold, then textures. Variants run in parallel
(asyncio.gather + Semaphore) with retries on 429/5xx and timeouts.
`seed` is documented as an accepted parameter but determinism is NOT guaranteed,
so no reproducibility is promised to the user.
"""
from __future__ import annotations

import asyncio
import logging
import random

from ..config import Config
from ..llm.openrouter import OpenRouter, OpenRouterError, image_data_url

logger = logging.getLogger(__name__)


class GenerationError(Exception):
    pass


class ImageClient:
    def __init__(self, client: OpenRouter, cfg: Config) -> None:
        self.client = client
        self.cfg = cfg

    def build_prompt(self, changes: str, materials: list[tuple[str, str]]) -> str:
        """materials: [(surface description, texture name)]"""
        lines = [
            "Reference 1 is the mold: the room image. It defines geometry, perspective, "
            "camera and composition. Keep walls, windows, doors and furniture positions "
            "exactly where they are; do not change the viewpoint.",
        ]
        for i, (surface, tex) in enumerate(materials, start=2):
            lines.append(f"Reference {i} is the {tex} texture: apply it to the {surface}.")
        lines.append(changes)
        if materials:
            names = ", ".join(f"{s} -> {t}" for s, t in materials)
            lines.append(f"Apply materials exactly as mapped: {names}. Match color, scale and "
                         "direction of each texture to real-world scale of the surface.")
        lines.append(
            "Photorealistic result, consistent lighting and shadows with the original scene, "
            "no people, no text, no watermarks."
        )
        return "\n".join(lines)

    async def generate_one(
        self,
        prompt: str,
        mold_jpeg: bytes,
        textures: list[bytes],
        *,
        aspect_ratio: str | None = None,
        timeout: float | None = None,
    ) -> bytes:
        refs = [{"type": "image_url", "image_url": {"url": image_data_url(mold_jpeg)}}]
        for tex in textures:
            refs.append({"type": "image_url", "image_url": {"url": image_data_url(tex)}})
        if len(refs) > 8:
            refs = refs[:8]
        resolution = self.cfg.image_resolution
        try:
            data, media, _usage = await self.client.generate_image(
                self.cfg.image_model,
                prompt,
                input_references=refs,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                seed=random.randint(0, 2**31 - 1),
                timeout=timeout or self.cfg.gen_timeout,
            )
        except OpenRouterError as e:
            # some models only accept the enum subset (e.g. 1K) -> one retry at 1K
            lowered = str(e).lower()
            if resolution != "1K" and "resolution" in lowered:
                logger.warning("resolution=%s rejected (%s), retrying at 1K", resolution, e)
                data, media, _usage = await self.client.generate_image(
                    self.cfg.image_model,
                    prompt,
                    input_references=refs,
                    aspect_ratio=aspect_ratio,
                    resolution="1K",
                    seed=random.randint(0, 2**31 - 1),
                    timeout=timeout or self.cfg.gen_timeout,
                )
            else:
                raise
        if media and "jpeg" not in media and "png" not in media:
            # K3 returns png/jpeg; anything else we still save
            logger.info("generated image media_type=%s", media)
        return data

    async def generate_variants(
        self,
        prompt: str,
        mold_jpeg: bytes,
        textures: list[bytes],
        n: int,
        *,
        aspect_ratio: str | None = None,
        progress_cb=None,
    ) -> list[tuple[int, bytes | None]]:
        """Generate n variants in parallel. Returns [(index, jpeg or None)]."""
        sem = asyncio.Semaphore(self.cfg.gen_concurrency)
        done = 0

        async def _one(idx: int) -> tuple[int, bytes | None]:
            async with sem:
                for attempt in range(3):
                    try:
                        img = await self.generate_one(
                            prompt, mold_jpeg, textures, aspect_ratio=aspect_ratio
                        )
                        return idx, img
                    except Exception as e:
                        wait = min(2.0**attempt * 1.5, 20.0) + random.uniform(0, 0.7)
                        logger.warning("variant %s attempt %s failed: %s", idx, attempt, e)
                        if attempt == 2:
                            return idx, None
                        await asyncio.sleep(wait)
                return idx, None

        async def _tracked(idx: int) -> tuple[int, bytes | None]:
            try:
                return await _one(idx)
            finally:
                nonlocal done
                done += 1
                if progress_cb:
                    try:
                        await progress_cb(done, n)
                    except Exception:
                        pass

        results = await asyncio.gather(*[_tracked(i) for i in range(n)])
        return results
