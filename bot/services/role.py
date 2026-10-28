"""Image role detection: caption first, then LLM (vision), then heuristic."""
from __future__ import annotations

import json
import logging

from ..config import Config
from ..llm.openrouter import OpenRouter, image_data_url
from ..llm.prompts import ROLE_CLASSIFIER_SYSTEM, ROLE_TOOL

logger = logging.getLogger(__name__)


class RoleClassifier:
    def __init__(self, client: OpenRouter, cfg: Config) -> None:
        self.client = client
        self.cfg = cfg

    async def classify(self, image_jpeg: bytes, hint: str | None = None) -> str:
        """Returns 'mold' | 'texture' | 'reference'; falls back to 'mold' on failure.

        `hint` is the user's free-text caption (any wording, no markup needed);
        it is passed to the model as a weak signal alongside the pixels.
        """
        user_text = "Classify this image."
        if hint:
            user_text += f"\nUser's note (may hint the role): {hint.strip()[:200]}"
        try:
            resp = await self.client.chat(
                self.cfg.llm_model,
                [
                    {"role": "system", "content": ROLE_CLASSIFIER_SYSTEM},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_text},
                            {
                                "type": "image_url",
                                "image_url": {"url": image_data_url(image_jpeg)},
                            },
                        ],
                    },
                ],
                tools=ROLE_TOOL,
                tool_choice={"type": "function", "function": {"name": "classify_role"}},
                reasoning={"effort": "low", "exclude": True},
                max_tokens=256,
                temperature=0.0,
            )
            msg = ((resp.get("choices") or [{}])[0].get("message") or {})
            calls = msg.get("tool_calls") or []
            if not calls:
                return "mold"
            args = calls[0].get("function", {}).get("arguments")
            data = args if isinstance(args, dict) else json.loads(args or "{}")
            role = str(data.get("role", "mold")).lower()
            return role if role in ("mold", "texture", "reference") else "mold"
        except Exception as e:
            logger.warning("role classification failed: %s", e)
            return "mold"
