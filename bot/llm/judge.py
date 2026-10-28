"""Judge: vision scoring of candidates (rubric via tool use + SSIM on edges)."""
from __future__ import annotations

import json
import logging

from pydantic import ValidationError

from ..config import Config
from .openrouter import OpenRouter, image_data_url
from .prompts import JUDGE_SYSTEM
from .schemas import JudgeScores, judge_tool_schema

logger = logging.getLogger(__name__)


def _extract_tool_args(resp: dict) -> dict | None:
    msg = ((resp.get("choices") or [{}])[0].get("message") or {})
    calls = msg.get("tool_calls") or []
    if not calls:
        return None
    fn = calls[0].get("function") or {}
    args = fn.get("arguments")
    if isinstance(args, dict):
        return args
    try:
        return json.loads(args or "{}")
    except (json.JSONDecodeError, TypeError):
        return None


class Judge:
    def __init__(self, client: OpenRouter, cfg: Config) -> None:
        self.client = client
        self.cfg = cfg

    async def score(
        self,
        mold_jpeg: bytes,
        textures: list[tuple[str, bytes]],
        candidate_jpeg: bytes,
        materials_desc: str,
    ) -> JudgeScores:
        content: list[dict] = [
            {
                "type": "text",
                "text": (
                    "Reference 1 is the mold. "
                    + (f"References 2..: textures used: {materials_desc}. " if textures else "")
                    + "The last image is the candidate. Score it."
                ),
            },
            {
                "type": "image_url",
                "image_url": {"url": image_data_url(mold_jpeg)},
            },
        ]
        for name, tex in textures:
            content.append({"type": "text", "text": f"Texture: {name}"})
            content.append({"type": "image_url", "image_url": {"url": image_data_url(tex)}})
        content.append({"type": "image_url", "image_url": {"url": image_data_url(candidate_jpeg)}})

        errors: list[str] = []
        for _attempt in range(2):
            msgs = [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": content}]
            if errors:
                msgs.append({"role": "user", "content": f"Previous answer invalid: {errors[-1]}. Retry."})
            try:
                resp = await self.client.chat(
                    self.cfg.llm_model,
                    msgs,
                    tools=judge_tool_schema(),
                    tool_choice={"type": "function", "function": {"name": "submit_scores"}},
                    reasoning={"effort": "low", "exclude": True},
                    max_tokens=1024,
                    temperature=0.0,
                )
            except Exception as e:
                errors.append(f"call failed: {e}")
                continue
            raw = _extract_tool_args(resp)
            if raw is None:
                errors.append("no tool call")
                continue
            try:
                return JudgeScores.model_validate(raw)
            except ValidationError as e:
                errors.append(f"validation: {e.errors()[-1] if e.errors() else e}")
        raise RuntimeError("judge failed after retry: " + (errors[-1] if errors else "unknown"))
