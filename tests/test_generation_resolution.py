"""Generation output resolution: 2K by default with a one-shot 1K fallback.

Regression: resolution was hardcoded to "1K" -> every saved/displayed result
was ~1024px ("отвратительное качество"). krea/k3-edit enum: 1K | 2K.
"""
from bot.config import Config
from bot.imaging.generate import ImageClient
from bot.llm.openrouter import OpenRouterError


class FakeClient:
    def __init__(self, reject_resolution: bool = False) -> None:
        self.calls: list[dict] = []
        self.reject_resolution = reject_resolution

    async def generate_image(self, model, prompt, **kwargs):
        self.calls.append(kwargs)
        if self.reject_resolution and kwargs.get("resolution") != "1K":
            raise OpenRouterError("openrouter error 400: invalid value for resolution")
        return b"png-bytes", "image/png", {}


def test_default_resolution_is_2k():
    assert Config(bot_token="t", openrouter_api_key="k").image_resolution == "2K"


async def test_generate_one_uses_configured_resolution():
    client = FakeClient()
    ic = ImageClient(client, Config(bot_token="t", openrouter_api_key="k"))

    data = await ic.generate_one("prompt", b"mold", [b"tex"])

    assert data == b"png-bytes"
    assert client.calls[0]["resolution"] == "2K"
    # references still passed (mold + texture)
    assert len(client.calls[0]["input_references"]) == 2


async def test_resolution_rejected_falls_back_to_1k():
    client = FakeClient(reject_resolution=True)
    ic = ImageClient(client, Config(bot_token="t", openrouter_api_key="k"))

    data = await ic.generate_one("prompt", b"mold", [])

    assert data == b"png-bytes"
    assert [c["resolution"] for c in client.calls] == ["2K", "1K"]


async def test_non_resolution_errors_do_not_downgrade():
    class FailClient:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def generate_image(self, model, prompt, **kwargs):
            self.calls.append(kwargs)
            raise OpenRouterError("openrouter error 500: upstream exploded")

    client = FailClient()
    ic = ImageClient(client, Config(bot_token="t", openrouter_api_key="k"))

    try:
        await ic.generate_one("p", b"m", [])
        raise AssertionError("must propagate the error")
    except OpenRouterError:
        pass
    assert len(client.calls) == 1  # no silent 1K downgrade for unrelated errors
