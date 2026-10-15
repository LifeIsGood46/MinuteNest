"""OpenAI-compatible provider for locally hosted servers.

Covers LM Studio (default http://localhost:1234/v1), vLLM, LocalAI,
llama.cpp server. Works without a key for local servers; pass a key
for hosted/protected ones.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from ..net import http_request, open_url, ssl_context, ssl_error_hint
from ..net import retry_after_seconds
from .base import CLOUD_PARALLEL, LLMProvider, LLMResponse, LLMError, call_with_retry, is_local_url
from .stream import Watch, post_streamed, read_openai_sse


class OpenAICompatProvider(LLMProvider):
    provider_name = "compat"

    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None, **kwargs):
        super().__init__(model, **kwargs)
        url = base_url or os.environ.get("COMPAT_BASE_URL") or "http://localhost:1234/v1"
        self.base_url = url.strip().rstrip("/")
        key = api_key or os.environ.get("COMPAT_API_KEY")
        self.api_key = key.strip() if key else None

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _http_error(self, e: urllib.error.HTTPError, *, generating: bool = True) -> LLMError:
        if e.code in (401, 403):
            return LLMError("Invalid API key for the OpenAI-compatible server. "
                            "Check the key in Settings (leave it blank for local servers).",
                            http_code=e.code)
        if e.code == 404 and generating:
            return LLMError(f"Model '{self.model or 'auto'}' not found on {self.base_url}. "
                            "Pick a model from the dropdown in Settings.")
        body = e.read().decode("utf-8", "replace")[:300]
        return LLMError(f"Request failed ({e.code}): {body}", http_code=e.code,
                        retry_after=retry_after_seconds(e))

    def _conn_error(self, e: Exception) -> LLMError:
        hint = ssl_error_hint(e)
        if hint:
            return LLMError(f"Connection to {self.base_url} failed: {hint}")
        return LLMError(f"Could not reach {self.base_url} — is LM Studio (or your server) running?")

    def _request(self, path: str, payload: dict | None = None, timeout: int = 600) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = http_request(f"{self.base_url}{path}", data=data, headers=self._headers(),
                                     method="POST" if payload is not None else "GET")
        try:
            with open_url(req, timeout=timeout, context=ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise self._http_error(e, generating=payload is not None) from e
        except Exception as e:
            raise self._conn_error(e) from e

    def list_models(self) -> list[str]:
        return sorted({m.get("id", "") for m in self._request("/models", timeout=10).get("data", [])} - {""})

    def verify_credentials(self, timeout: int = 8) -> None:  # noqa: ARG002 - /models has its own limit
        """Cheap /models fetch validates reachability + key."""
        self.list_models()

    def _resolve_model(self) -> str:
        """Empty model name -> first model the server offers (LM Studio zero-config)."""
        if self.model:
            return self.model
        models = self.list_models()
        if not models:
            raise LLMError(
                f"No models are served at {self.base_url}. "
                "Load a model in LM Studio (or your server) and refresh."
            )
        return models[0]

    def complete(self, system: str, user: str, *, max_tokens: int = 8000,
                 watch: Watch | None = None) -> LLMResponse:
        model = self._resolve_model()
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "stream": True,
        }
        return call_with_retry(lambda: post_streamed(
            f"{self.base_url}/chat/completions", self._headers(), payload, read_openai_sse,
            provider=self, model=model, watch=watch,
            http_error=self._http_error, conn_error=self._conn_error))

    def is_local(self) -> bool:
        return is_local_url(self.base_url)

    def parallel_requests(self) -> int:
        return 1 if self.is_local() else CLOUD_PARALLEL

    def is_available(self) -> bool:
        try:
            self.list_models()
            return True
        except LLMError:
            return False
