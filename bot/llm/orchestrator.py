"""Brief orchestrator: Haiku 5.5 -> strict JSON plan via tool use."""
from __future__ import annotations

import json
import logging

from pydantic import ValidationError

from ..config import Config
from .openrouter import OpenRouter
from .prompts import ORCHESTRATOR_SYSTEM
from .schemas import Plan, orchestrator_tool_schema

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


def build_orchestrator_messages(
    brief: str,
    files: list[dict],
    instructions: list[str],
    history: list[dict],
    default_variants: int,
    viewer_notes: list[str] | None = None,
    attached_files_desc: str | None = None,
) -> list[dict]:
    def _file_line(f: dict) -> str:
        line = f"- id={f['id']} kind={f['kind']} caption={f.get('caption') or '-'}"
        if f.get("analysis"):
            line += f" | sees: {f['analysis']}"
        return line

    files_desc = "\n".join(_file_line(f) for f in files) or "(no files yet)"
    instr = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(instructions)) or "(none)"
    hist = "\n".join(
        f"- step {h['id']}: brief={h['brief_text'][:120]!r} winner_file={h['winner_file_id']}"
        for h in history[-6:]
    ) or "(empty)"
    system = ORCHESTRATOR_SYSTEM
    if instructions:
        system += f"\n\n<user_instructions>\nPermanent instructions from the user (follow them):\n{instr}\n</user_instructions>"
    viewer_take = ""
    if viewer_notes:
        viewer_take = "\n\nViewer's synthesis (what the images show and how to apply):\n" + "\n".join(viewer_notes)
    attached_block = ""
    if attached_files_desc:
        attached_block = (
            "\n\nAttached to this message: " + attached_files_desc + ". "
            "If the brief asks to edit or change an existing picture, prefer the "
            "attached file as base_file_id."
        )
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                f"Project files:\n{files_desc}\n\n"
                f"Step history (winner_file_id of the last step is the default base):\n{hist}\n\n"
                f"Default n_variants: {default_variants}\n\n"
                f"User brief (may be raw voice transcription):\n{brief}{viewer_take}{attached_block}"
            ),
        },
    ]


class Orchestrator:
    def __init__(self, client: OpenRouter, cfg: Config) -> None:
        self.client = client
        self.cfg = cfg

    async def plan(self, messages: list[dict]) -> Plan:
        """Call LLM, validate with Pydantic; one repair-retry on validation error."""
        errors: list[str] = []
        for _attempt in range(2):
            msgs = list(messages)
            if errors:
                msgs.append(
                    {
                        "role": "user",
                        "content": f"Your previous answer was invalid: {errors[-1]}. "
                        "Fix it and call submit_plan again.",
                    }
                )
            try:
                resp = await self.client.chat(
                    self.cfg.llm_model,
                    msgs,
                    tools=orchestrator_tool_schema(),
                    tool_choice={"type": "function", "function": {"name": "submit_plan"}},
                    reasoning={"effort": "low", "exclude": True},
                    max_tokens=2048,
                    temperature=0.2,
                )
            except Exception as e:
                errors.append(f"LLM call failed: {e}")
                logger.warning("orchestrator call failed: %s", e)
                continue
            raw = _extract_tool_args(resp)
            if raw is None:
                errors.append("no tool call in response")
                continue
            try:
                plan = Plan.model_validate(raw)
                return self._clamp(plan)
            except ValidationError as e:
                errors.append(f"validation: {e.errors()[-1] if e.errors() else e}")
                continue
            except Exception as e:
                errors.append(str(e)[:400])
                continue
        raise RuntimeError(
            "orchestrator produced invalid plan after retry: " + (errors[-1] if errors else "unknown")
        )

    def _clamp(self, plan: Plan) -> Plan:
        for job in plan.jobs:
            if job.n_variants > self.cfg.max_variants:
                job.n_variants = self.cfg.max_variants
            if len(job.materials) > self.cfg.max_texture_refs:
                job.materials = job.materials[: self.cfg.max_texture_refs]
        return plan
