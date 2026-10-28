"""OpenRouter client: chat completions (w/ tools, reasoning, streaming),
Image API (krea/k3-edit) and audio transcriptions. httpx async + retries."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
from collections.abc import AsyncIterator
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class OpenRouterError(Exception):
    pass


class OpenRouter:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        *,
        timeout: float = 120.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/telesign",
                "X-Title": "Telesign interior bot",
            },
            timeout=httpx.Timeout(timeout, connect=30.0),
        )
        self._sem = asyncio.Semaphore(4)

    async def close(self) -> None:
        await self._client.aclose()

    # --- low level --------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        timeout: float | None = None,
        max_retries: int = 4,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                async with self._sem:
                    resp = await self._client.request(
                        method, path, json=json_body, timeout=timeout or self._client.timeout
                    )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                if attempt < max_retries:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise OpenRouterError(f"network error: {e}") from e

            if resp.status_code in RETRYABLE_STATUSES and attempt < max_retries:
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after else self._backoff(attempt)
                logger.info("openrouter %s %s -> retry %s in %.1fs", method, path, resp.status_code, delay)
                await asyncio.sleep(delay)
                continue

            if resp.status_code >= 400:
                try:
                    err = resp.json().get("error", {})
                    msg = err.get("message", resp.text[:300])
                    code = err.get("code", resp.status_code)
                except Exception:
                    msg, code = resp.text[:300], resp.status_code
                raise OpenRouterError(f"openrouter error {code}: {msg}")

            data = resp.json()
            if "error" in data and data.get("error"):
                raise OpenRouterError(f"openrouter error: {data['error']}")
            return data
        raise OpenRouterError(f"retries exhausted: {last_exc}")

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2.0**attempt * 0.5, 15.0) + random.uniform(0, 0.5)

    # --- chat -------------------------------------------------------------
    async def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        response_format: dict | None = None,
        reasoning: dict | None = None,
        max_tokens: int = 2048,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
        if response_format:
            body["response_format"] = response_format
        if reasoning:
            body["reasoning"] = reasoning
        if temperature is not None:
            body["temperature"] = temperature
        return await self._request("POST", "/chat/completions", json_body=body)

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        *,
        reasoning: dict | None = None,
        max_tokens: int = 2048,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """Yield content deltas from an SSE chat stream."""
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if reasoning:
            body["reasoning"] = reasoning
        if temperature is not None:
            body["temperature"] = temperature

        for attempt in range(3):
            try:
                async with self._client.stream("POST", "/chat/completions", json=body) as resp:
                    if resp.status_code in RETRYABLE_STATUSES:
                        await asyncio.sleep(self._backoff(attempt))
                        continue
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode("utf-8", "replace")[:300]
                        raise OpenRouterError(f"stream error {resp.status_code}: {text}")
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:]
                        if payload.strip() == "[DONE]":
                            return
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("error"):
                            raise OpenRouterError(f"stream error: {chunk['error']}")
                        choices = chunk.get("choices") or []
                        delta = (choices[0] or {}).get("delta", {}) if choices else {}
                        content = delta.get("content")
                        if content:
                            yield content
                return
            except (httpx.TransportError, httpx.StreamError) as e:
                last_exc = e
                if attempt < 2:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise OpenRouterError(f"stream network error: {e}") from e
        raise OpenRouterError(f"stream retries exhausted: {last_exc}")

    # --- images -----------------------------------------------------------
    async def generate_image(
        self,
        model: str,
        prompt: str,
        *,
        input_references: list[dict] | None = None,
        aspect_ratio: str | None = None,
        resolution: str | None = "1K",
        seed: int | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, str, dict]:
        """POST /images -> (image_bytes, media_type, usage)."""
        body: dict[str, Any] = {"model": model, "prompt": prompt}
        if input_references:
            body["input_references"] = input_references
        if aspect_ratio:
            body["aspect_ratio"] = aspect_ratio
        if resolution:
            body["resolution"] = resolution
        if seed is not None:
            body["seed"] = seed
        data = await self._request(
            "POST", "/images", json_body=body, timeout=timeout or 300.0, max_retries=3
        )
        items = data.get("data") or []
        if not items:
            raise OpenRouterError("image response has no data")
        b64 = items[0].get("b64_json")
        if not b64:
            raise OpenRouterError("image response has no b64_json")
        media = items[0].get("media_type") or "image/png"
        return base64.b64decode(b64), media, data.get("usage") or {}

    # --- transcriptions ---------------------------------------------------
    async def transcribe(
        self,
        audio_bytes: bytes,
        audio_format: str,
        *,
        model: str,
        language: str | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "model": model,
            "input_audio": {"data": base64.b64encode(audio_bytes).decode(), "format": audio_format},
        }
        if language:
            body["language"] = language
        data = await self._request("POST", "/audio/transcriptions", json_body=body, max_retries=2)
        return (data.get("text") or "").strip()

    # --- service ----------------------------------------------------------
    async def list_models(self) -> dict:
        return await self._request("GET", "/models")

    async def model_available(self, model: str) -> bool:
        try:
            data = await self.list_models()
        except OpenRouterError:
            return False
        return any(m.get("id") == model for m in data.get("data", []))


def image_data_url(image_bytes: bytes, media_type: str = "image/jpeg") -> str:
    return f"data:{media_type};base64,{base64.b64encode(image_bytes).decode()}"
