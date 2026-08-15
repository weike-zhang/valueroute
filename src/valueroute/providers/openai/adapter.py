from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from valueroute.observability.usage import CostStatus, UsageRecord
from valueroute.domain.models import new_id


class ProviderCallError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class ProviderResult:
    text: str
    usage: UsageRecord
    raw: dict[str, Any]


class OpenAIProviderAdapter:
    """OpenAI-compatible Responses adapter; secrets stay outside domain state."""

    def __init__(self, *, model_id: str, provider_id: str = "openai", base_url: str = "https://api.openai.com/v1", api_key: str | None = None, client: httpx.AsyncClient | None = None):
        self.provider_id = provider_id
        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY")
        self.client = client

    async def complete(self, *, task_id: str, input_text: str, reasoning_effort: str = "medium", retries: int = 0) -> ProviderResult:
        if not self.api_key:
            raise ProviderCallError("provider credential is not configured", retryable=False)
        payload = {"model": self.model_id, "input": input_text, "reasoning": {"effort": reasoning_effort}}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        started = time.perf_counter()
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=60.0)
        performed_retries = 0
        try:
            while True:
                try:
                    try:
                        response = await client.post(f"{self.base_url}/responses", json=payload, headers=headers)
                    except httpx.TimeoutException as error:
                        raise ProviderCallError("provider timeout", retryable=True) from error
                    except httpx.HTTPError as error:
                        raise ProviderCallError("provider transport error", retryable=True) from error
                    if response.status_code >= 400:
                        raise ProviderCallError(f"provider returned HTTP {response.status_code}", retryable=response.status_code >= 500, status_code=response.status_code)
                    raw = response.json()
                    usage_data = raw.get("usage") or {}
                    usage = UsageRecord(
                        id=new_id("usage"), task_id=task_id, provider_id=self.provider_id, model_id=self.model_id,
                        input_tokens=usage_data.get("input_tokens"), cached_input_tokens=usage_data.get("input_tokens_details", {}).get("cached_tokens"),
                        output_tokens=usage_data.get("output_tokens"), reasoning_tokens=usage_data.get("reasoning_tokens"),
                        cost_status=CostStatus.unknown, latency_ms=int((time.perf_counter() - started) * 1000), retries=performed_retries,
                    )
                    return ProviderResult(text=_response_text(raw), usage=usage, raw=raw)
                except ProviderCallError as error:
                    if not error.retryable or performed_retries >= retries:
                        raise
                    performed_retries += 1
                    await asyncio.sleep(min(0.25 * (2 ** (performed_retries - 1)), 2.0))
        finally:
            if own_client:
                await client.aclose()


def _response_text(raw: dict[str, Any]) -> str:
    if isinstance(raw.get("output_text"), str):
        return raw["output_text"]
    chunks: list[str] = []
    for item in raw.get("output", []) or []:
        for content in item.get("content", []) or []:
            if isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "".join(chunks)
