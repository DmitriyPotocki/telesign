from __future__ import annotations

import pathlib
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    openrouter_api_key: str = ""

    # raw string on purpose: pydantic-settings would try to JSON-decode complex types;
    # we accept "123,456" / "123 456" forms ourselves
    allowed_user_ids: str = Field(default="")

    llm_model: str = "anthropic/claude-haiku-5.5"
    image_model: str = "krea/k3-edit"
    image_resolution: str = "2K"  # 1K | 2K (k3-edit enum); falls back to 1K if rejected
    default_variants: int = 4
    max_variants: int = 8
    daily_image_limit: int = 60

    stt_provider: str = "cloud"  # local | cloud
    stt_model: str = "mistralai/voxtral-small-transcribe-3-0"
    whisper_model_size: str = "small"
    max_voice_seconds: int = 120

    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    data_dir: pathlib.Path = pathlib.Path("data")

    # HTTP health stub (Render/any PaaS): reads PORT from the environment,
    # Render assigns it automatically; local default below.
    port: int = 8080

    # generation tuning
    gen_concurrency: int = 4
    gen_timeout: float = 240.0
    step_timeout: float = 900.0
    judge_weight_rubric: float = 0.65
    judge_weight_metric: float = 0.35
    quality_threshold: float = 6.0

    max_texture_refs: int = 7  # + 1 mold = 8 (K3 accepts up to 8 refs)
    max_instructions: int = 20
    max_instruction_len: int = 500

    @model_validator(mode="after")
    def _normalize(self) -> Config:
        if isinstance(self.allowed_user_ids, list | tuple):  # pragma: no cover
            self.allowed_user_ids = ",".join(map(str, self.allowed_user_ids))
        if self.default_variants < 1:
            self.default_variants = 1
        if self.max_variants < self.default_variants:
            self.max_variants = self.default_variants
        return self

    @property
    def whitelist(self) -> frozenset[int]:
        return frozenset(
            int(x) for x in self.allowed_user_ids.replace(",", " ").split() if x.strip().isdigit()
        )


@lru_cache
def get_config() -> Config:
    return Config()


config = get_config()
