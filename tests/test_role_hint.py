"""No-markup UX: role hint from free-text captions + help/prompt wording."""
from bot.config import Config
from bot.handlers.common import HELP_TEXT
from bot.llm.prompts import ORCHESTRATOR_SYSTEM
from bot.services.role import RoleClassifier


class FakeClient:
    """Captures the messages sent to the LLM and returns a fixed tool call."""

    def __init__(self, role: str = "texture") -> None:
        self.role = role
        self.messages = None

    async def chat(self, model, messages, **kwargs):
        self.messages = messages
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "classify_role",
                                    "arguments": {"role": self.role, "reason": None},
                                }
                            }
                        ]
                    }
                }
            ]
        }


def _cfg() -> Config:
    return Config(bot_token="t", openrouter_api_key="k")


async def test_free_text_caption_passed_as_hint():
    client = FakeClient()
    role = await RoleClassifier(client, _cfg()).classify(b"img", hint="дуб тёмный, на пол")
    assert role == "texture"
    user_text = client.messages[1]["content"][0]["text"]
    assert "дуб тёмный, на пол" in user_text


async def test_no_caption_no_hint():
    client = FakeClient(role="mold")
    role = await RoleClassifier(client, _cfg()).classify(b"img")
    assert role == "mold"
    user_text = client.messages[1]["content"][0]["text"]
    assert user_text == "Classify this image."


async def test_long_hint_is_truncated():
    client = FakeClient()
    await RoleClassifier(client, _cfg()).classify(b"img", hint="х" * 500)
    user_text = client.messages[1]["content"][0]["text"]
    assert len(user_text) < 400  # hint capped at 200 chars


def test_help_needs_no_markup():
    assert "<code>молд:" not in HELP_TEXT
    assert "Никакая разметка не нужна" in HELP_TEXT
    assert "поверхность" not in HELP_TEXT  # no "surface -> texture" format requirement


def test_orchestrator_accepts_free_form_briefs():
    assert "free-form natural language" in ORCHESTRATOR_SYSTEM
    assert "Do not ask just to confirm a sensible inference" in ORCHESTRATOR_SYSTEM
