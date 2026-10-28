import asyncio
import json

import pytest
from pydantic import ValidationError

from bot.config import Config
from bot.llm.orchestrator import Orchestrator
from bot.llm.schemas import Plan

VALID_ARGS = {
    "action": "generate",
    "question": None,
    "jobs": [
        {
            "base_file_id": 12,
            "n_variants": 4,
            "prompt": "apply oak to floor, keep geometry",
            "aspect_ratio": None,
            "materials": [{"surface": "пол", "texture_file_id": 15}],
        }
    ],
}


def tool_response(args):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "submit_plan", "arguments": json.dumps(args, ensure_ascii=False)},
                        }
                    ],
                }
            }
        ]
    }


def test_valid_plan_parses():
    plan = Plan.model_validate(VALID_ARGS)
    assert plan.action == "generate"
    assert plan.jobs[0].materials[0].surface == "пол"


def test_invalid_n_variants_fails():
    bad = json.loads(json.dumps(VALID_ARGS))
    bad["jobs"][0]["n_variants"] = 0
    with pytest.raises(ValidationError):
        Plan.model_validate(bad)


def test_ask_clarification_requires_question():
    bad = {"action": "ask_clarification", "question": "", "jobs": []}
    with pytest.raises(ValidationError):
        Plan.model_validate(bad)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, model, messages, **kw):
        self.calls.append(messages)
        return self.responses.pop(0)


def test_orchestrator_repair_retry():
    async def go():
        cfg = Config(bot_token="t", openrouter_api_key="k")
        client = FakeClient([tool_response({"action": "bogus", "jobs": []}), tool_response(VALID_ARGS)])
        orch = Orchestrator(client, cfg)
        plan = await orch.plan([{"role": "user", "content": "x"}])
        return plan, len(client.calls)

    plan, calls = asyncio.run(go())
    assert plan.action == "generate"
    assert calls == 2  # one invalid + one repair retry


def test_orchestrator_gives_up_after_retry():
    async def go():
        cfg = Config(bot_token="t", openrouter_api_key="k")
        client = FakeClient([tool_response({"action": "bogus", "jobs": []}), tool_response({"action": "bogus", "jobs": []})])
        orch = Orchestrator(client, cfg)
        with pytest.raises(RuntimeError):
            await orch.plan([{"role": "user", "content": "x"}])

    asyncio.run(go())


def test_orchestrator_clamps_variants():
    async def go():
        cfg = Config(bot_token="t", openrouter_api_key="k", max_variants=3, default_variants=2)
        big = json.loads(json.dumps(VALID_ARGS))
        big["jobs"][0]["n_variants"] = 99
        client = FakeClient([tool_response(big)])
        orch = Orchestrator(client, cfg)
        plan = await orch.plan([{"role": "user", "content": "x"}])
        return plan

    plan = asyncio.run(go())
    assert plan.jobs[0].n_variants == 3


def test_orchestrator_no_tool_call_is_error():
    async def go():
        cfg = Config(bot_token="t", openrouter_api_key="k")
        client = FakeClient([{"choices": [{"message": {"content": "просто текст"}}]}])
        orch = Orchestrator(client, cfg)
        with pytest.raises(RuntimeError):
            await orch.plan([{"role": "user", "content": "x"}])

    asyncio.run(go())


def test_system_prompts_forbid_plastic_look():
    """The 'plastic AI look' must be excluded in both system prompts."""
    from bot.llm.prompts import JUDGE_SYSTEM, ORCHESTRATOR_SYSTEM

    # orchestrator: the image prompt it writes demands real materials
    assert "plastic" in ORCHESTRATOR_SYSTEM
    assert "micro-roughness" in ORCHESTRATOR_SYSTEM
    assert "doll-like" in ORCHESTRATOR_SYSTEM
    # judge: plastic/waxy look is penalized as an artifact
    assert "plastic/waxy look" in JUDGE_SYSTEM
    assert "lowers `artifacts` and `textures`" in JUDGE_SYSTEM
