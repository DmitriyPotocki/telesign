"""The judge must score from REAL images (vision), never from text descriptions.

Regression guard: judge.score embeds base64 images of the mold, every texture
and the candidate itself into the vision request — viewer descriptions are
only fed to the orchestrator (planning), never to the jury.
"""
import base64
import json

from bot.config import Config
from bot.llm.judge import Judge


class FakeClient:
    def __init__(self) -> None:
        self.messages = None
        self.kwargs = None

    async def chat(self, model, messages, **kwargs):
        self.messages = messages
        self.kwargs = kwargs
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "submit_scores",
                                    "arguments": json.dumps(
                                        {"geometry": 8, "textures": 7, "lighting": 9, "artifacts": 10}
                                    ),
                                }
                            }
                        ]
                    }
                }
            ]
        }


def _user_content(client: FakeClient) -> list[dict]:
    return client.messages[-1]["content"]


async def test_judge_receives_mold_texture_and_candidate_images():
    client = FakeClient()
    judge = Judge(client, Config(bot_token="t", openrouter_api_key="k"))

    mold, tex, cand = b"mold-bytes", b"texture-bytes", b"candidate-bytes"
    scores = await judge.score(
        mold, [("пол", tex)], cand, materials_desc="пол <- дуб"
    )

    assert scores.geometry == 8 and scores.artifacts == 10
    content = _user_content(client)
    images = [c for c in content if c.get("type") == "image_url"]

    # mold + 1 texture + candidate — actual pixels, not prose
    assert len(images) == 3
    assert base64.b64encode(mold).decode() in images[0]["image_url"]["url"]
    assert base64.b64encode(tex).decode() in images[1]["image_url"]["url"]
    # the candidate being judged is the LAST image
    assert base64.b64encode(cand).decode() in images[-1]["image_url"]["url"]


async def test_judge_scoring_is_forced_tool_call():
    client = FakeClient()
    judge = Judge(client, Config(bot_token="t", openrouter_api_key="k"))
    await judge.score(b"m", [], b"c", materials_desc="")

    assert client.kwargs["tool_choice"]["function"]["name"] == "submit_scores"
    assert client.kwargs["temperature"] == 0.0


async def test_judge_without_textures_still_sees_both_images():
    client = FakeClient()
    judge = Judge(client, Config(bot_token="t", openrouter_api_key="k"))
    await judge.score(b"m", [], b"c", materials_desc="")

    images = [c for c in _user_content(client) if c.get("type") == "image_url"]
    assert len(images) == 2  # mold + candidate
