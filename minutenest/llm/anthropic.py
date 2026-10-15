"""Anthropic (Claude) provider — pure REST, no SDK needed (works in the frozen exe)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from ..net import http_request, open_url, retry_after_seconds, ssl_context, ssl_error_hint
from .base import CLOUD_PARALLEL, LLMProvider, LLMResponse, LLMError, call_with_retry
from .stream import Watch, post_streamed, read_anthropic_sse

API_BASE = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"
MAX_MODEL_PAGES = 20   # pages of up to 1000 models; Anthropic lists a few dozen


class AnthropicProvider(LLMProvider):
    provider_name = "anthropic"

    def __init__(self, model: str, api_key: str | None = None, **kwargs):
        super().__init__(model, **kwargs)
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.api_key = key.strip() if key else None
        # ANTHROPIC_BASE_URL: the official SDKs' override (a proxy or gateway; tests).
        # Like the SDKs, it names the host ("https://api.anthropic.com"), without /v1.
        root = os.environ.get("ANTHROPIC_BASE_URL")
        self.base_url = f"{root.rstrip('/')}/v1" if root else API_BASE

    def _headers(self) -> dict:
        if not self.api_key:
            raise LLMError("Anthropic API key is not set. Open Settings and paste your key.")
        return {"Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": API_VERSION}

    def _http_error(self, e: urllib.error.HTTPError) -> LLMError:
        if e.code in (401, 403):
            return LLMError("Invalid or expired Anthropic API key. Open Settings, paste a fresh key.",
                            http_code=e.code)
        if e.code == 429:
            return LLMError("Anthropic rate limit hit (your key is valid) — wait a bit and retry.",
                            http_code=e.code, retry_after=retry_after_seconds(e))
        if e.code == 404:
            return LLMError(f"Anthropic model '{self.model}' not found.", http_code=e.code)
        body = e.read().decode("utf-8", "replace")[:300]
        return LLMError(f"Anthropic request failed ({e.code}): {body}", http_code=e.code)

    @staticmethod
    def _conn_error(e: Exception) -> LLMError:
        hint = ssl_error_hint(e)
        if hint:
            return LLMError(f"Anthropic connection failed: {hint}")
        return LLMError(f"Could not reach Anthropic: {e}. Check connection.")

    def _request(self, path: str, payload: dict | None = None, timeout: int = 600) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = http_request(f"{self.base_url}{path}", data=data, headers=self._headers(),
                                     method="POST" if payload is not None else "GET")
        try:
            with open_url(req, timeout=timeout, context=ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise self._http_error(e) from e
        except LLMError:
            raise
        except Exception as e:
            raise self._conn_error(e) from e

    def list_models(self) -> list[str]:
        """Every model the key can use: the list is paged (20 per page by default)."""
        ids: list[str] = []
        query = "?limit=1000"
        for _ in range(MAX_MODEL_PAGES):        # bounded: a server that always says has_more can't loop us
            page = self._request(f"/models{query}", timeout=15)
            ids += [m.get("id", "") for m in page.get("data", [])]
            if not page.get("has_more") or not page.get("last_id"):
                break
            query = f"?limit=1000&after_id={page['last_id']}"
        return sorted({i for i in ids if i})

    def verify_credentials(self, timeout: int = 30) -> None:
        """GET /models doubles as an auth probe — no tokens burned."""
        self._request("/models", timeout=timeout)

    def complete(self, system: str, user: str, *, max_tokens: int = 16000,
                 watch: Watch | None = None) -> LLMResponse:
        # 16000: current Claude models think before answering (always on for
        # Opus 5.5) and thinking counts toward max_tokens.
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "stream": True,
        }
        return call_with_retry(lambda: post_streamed(
            f"{self.base_url}/messages", self._headers(), payload, read_anthropic_sse,
            provider=self, model=self.model, watch=watch,
            http_error=self._http_error, conn_error=self._conn_error))

    def is_available(self) -> bool:
        return bool(self.api_key)

    def parallel_requests(self) -> int:
        return CLOUD_PARALLEL
