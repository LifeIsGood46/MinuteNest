"""OpenAI provider — pure REST, no SDK needed (works in the frozen exe)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from ..net import http_request, open_url, retry_after_seconds, ssl_context, ssl_error_hint
from .base import CLOUD_PARALLEL, LLMProvider, LLMResponse, LLMError, call_with_retry
from .stream import Watch, post_streamed, read_openai_sse

API_BASE = "https://api.openai.com/v1"

# Prefixes of chat-capable models; anything else (embeddings, TTS, image…)
# is hidden from the picker (Custom... still allows them).
_CHAT_PREFIXES = ("gpt", "o1", "o3", "o4", "chatgpt")
# ...minus the GPT-named models that can't write a document over Chat
# Completions (speech, voice, images, search, moderation).
_NOT_CHAT = ("transcribe", "tts", "realtime", "audio", "image", "search", "embedding", "moderation")
# Output budget including reasoning tokens (GPT-5/6 and o-series think first).
MAX_OUTPUT_TOKENS = 16000


class OpenAIProvider(LLMProvider):
    provider_name = "openai"

    def __init__(self, model: str, api_key: str | None = None, **kwargs):
        super().__init__(model, **kwargs)
        key = api_key or os.environ.get("OPENAI_API_KEY")
        self.api_key = key.strip() if key else None
        # OPENAI_BASE_URL: the official SDKs' override (a proxy or gateway; tests)
        self.base_url = (os.environ.get("OPENAI_BASE_URL") or API_BASE).rstrip("/")

    def _headers(self) -> dict:
        if not self.api_key:
            raise LLMError("OpenAI API key is not set. Open Settings and paste your key.")
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}

    def _http_error(self, e: urllib.error.HTTPError) -> LLMError:
        if e.code in (401, 403):
            return LLMError("Invalid or expired OpenAI API key. Open Settings, paste a fresh key.",
                            http_code=e.code)
        if e.code == 429:
            return LLMError("OpenAI rate limit hit (your key is valid) — wait a bit and retry.",
                            http_code=e.code, retry_after=retry_after_seconds(e))
        if e.code == 404:
            return LLMError(f"OpenAI model '{self.model}' not found for your key.", http_code=e.code)
        body = e.read().decode("utf-8", "replace")[:300]
        return LLMError(f"OpenAI request failed ({e.code}): {body}", http_code=e.code)

    @staticmethod
    def _conn_error(e: Exception) -> LLMError:
        hint = ssl_error_hint(e)
        if hint:
            return LLMError(f"OpenAI connection failed: {hint}")
        return LLMError(f"Could not reach OpenAI: {e}. Check connection.")

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
        ids = [m.get("id", "") for m in self._request("/models", timeout=15).get("data", [])]
        ids = sorted({i for i in ids if i})
        chat = [i for i in ids if i.startswith(_CHAT_PREFIXES) and not any(k in i for k in _NOT_CHAT)]
        return chat or ids

    def verify_credentials(self, timeout: int = 30) -> None:
        """GET /models doubles as an auth probe — no tokens burned."""
        self._request("/models", timeout=timeout)

    def complete(self, system: str, user: str, *, max_tokens: int = MAX_OUTPUT_TOKENS,
                 watch: Watch | None = None) -> LLMResponse:
        # max_completion_tokens, not max_tokens: the reasoning models (GPT-5/6,
        # o-series) reject the old name with a 400; every chat model accepts this.
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_completion_tokens": max_tokens,
            "stream": True,
        }
        return call_with_retry(lambda: post_streamed(
            f"{self.base_url}/chat/completions", self._headers(), payload, read_openai_sse,
            provider=self, model=self.model, watch=watch,
            http_error=self._http_error, conn_error=self._conn_error))

    def is_available(self) -> bool:
        return bool(self.api_key)

    def parallel_requests(self) -> int:
        return CLOUD_PARALLEL
