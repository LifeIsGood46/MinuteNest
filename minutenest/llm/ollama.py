"""Ollama provider — local daemon (native API) or Ollama cloud (OpenAI-compatible)."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from ..net import error_detail, http_request, open_url, retry_after_seconds, ssl_context, ssl_error_hint
from .base import CLOUD_PARALLEL, LLMProvider, LLMResponse, LLMError, call_with_retry, is_local_url
from .stream import Watch, post_streamed, read_ollama_ndjson, read_openai_sse

log = logging.getLogger("minutenest.llm.ollama")


class OllamaProvider(LLMProvider):
    provider_name = "ollama"

    def __init__(self, model: str, host: str | None = None, api_key: str | None = None, **kwargs):
        super().__init__(model, **kwargs)
        host = host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
        self.host = host.strip().rstrip("/")
        key = api_key or os.environ.get("OLLAMA_API_KEY")
        self.api_key = key.strip() if key else None

    @property
    def _is_remote(self) -> bool:
        """Host points somewhere other than this machine (cloud or LAN box)."""
        from urllib.parse import urlparse

        try:
            hostname = (urlparse(self.host).hostname or "").lower()
        except ValueError:      # a malformed address (bad port, brackets): treated as local
            return False
        return hostname not in ("", "localhost", "127.0.0.1", "::1")

    @property
    def _is_cloud(self) -> bool:
        return bool(self.api_key)

    def _require_key_for_remote(self) -> None:
        """A remote host without a key silently hits the wrong API shape.

        Ollama Cloud needs the OpenAI-compatible endpoint + key; without one
        the request would go to the native daemon API on a remote server and
        fail confusingly (or, worse, hit an unauthenticated endpoint).
        """
        if self._is_remote and not self.api_key:
            raise LLMError(
                f"Ollama host '{self.host}' is remote but no API key is set. "
                "Paste an Ollama API key in Settings, or use the Local host."
            )

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _http_error(self, e: urllib.error.HTTPError, path: str = "") -> LLMError:
        # Read the body ONCE (the stream is consumed by read()).
        body = e.read().decode("utf-8", "replace")[:300]
        log.error("ollama HTTP %s on %s: %s", e.code, path, body)
        if e.code in (401, 403):
            return LLMError("Invalid or expired Ollama API key. Open Settings, paste a fresh key.",
                            http_code=e.code)
        if e.code == 429:
            return LLMError(
                "Ollama cloud usage limit reached (your key is valid). Wait for the "
                "limit to reset, upgrade the plan, or switch to a local model."
                + (f" Details: {error_detail(body)}" if body.strip() else ""),
                http_code=e.code, retry_after=retry_after_seconds(e))
        if e.code == 402:
            return LLMError(
                "Ollama cloud: this model is not included in your current usage plan "
                "(your key is valid). Pick another model, or add usage credits."
                + (f" Details: {error_detail(body)}" if body.strip() else ""),
                http_code=e.code)
        return LLMError(f"Ollama request failed ({e.code}): {error_detail(body)}",
                        http_code=e.code, retry_after=retry_after_seconds(e))

    def _conn_error(self, e: Exception) -> LLMError:
        hint = ssl_error_hint(e)
        if hint:
            log.error("ollama TLS failure: %s", e)
            return LLMError(f"Ollama connection failed: {hint}")
        log.error("ollama request failed: %s", e)
        hint = "Is `ollama serve` running?" if not self._is_cloud else "Check host/key."
        return LLMError(f"Ollama request failed ({e}). {hint}")

    def _request(self, path: str, payload: dict | None = None, timeout: int = 600) -> dict:
        """POST/GET helper with bearer auth when a key is set; maps HTTP errors."""
        self._require_key_for_remote()
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = http_request(f"{self.host}{path}", data=data, headers=self._headers(),
                                     method="POST" if payload is not None else "GET")
        log.debug("ollama request: %s %s (auth=%s)", req.get_method(), req.full_url,
                  bool(self.api_key))
        try:
            with open_url(req, timeout=timeout, context=ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise self._http_error(e, path) from e
        except Exception as e:
            raise self._conn_error(e) from e

    def complete(self, system: str, user: str, *, max_tokens: int = 8000,
                 watch: Watch | None = None) -> LLMResponse:
        self._require_key_for_remote()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if self._is_cloud:
            path, parse = "/v1/chat/completions", read_openai_sse
            payload: dict = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                             "stream": True}
        else:
            path, parse = "/api/chat", read_ollama_ndjson
            payload = {"model": self.model, "messages": messages, "stream": True,
                       "options": {"num_predict": max_tokens}}
        return call_with_retry(lambda: post_streamed(
            f"{self.host}{path}", self._headers(), payload, parse, provider=self, model=self.model,
            watch=watch, http_error=lambda e: self._http_error(e, path), conn_error=self._conn_error))

    def list_models(self) -> list[str]:
        # Cloud accounts expose models on the OpenAI-compatible endpoint;
        # /api/tags is the local daemon's native API.
        if self._is_cloud:
            data = self._request("/v1/models", timeout=15)
            return sorted({m.get("id", "") for m in data.get("data", [])} - {""})
        data = self._request("/api/tags", timeout=15)
        return sorted({m.get("name") or m.get("model") or "" for m in data.get("models", [])} - {""})

    def verify_credentials(self, timeout: int = 30) -> None:
        """1-token chat probe; /api/tags is public so it can't detect bad keys."""
        if not self._is_cloud:
            self._require_key_for_remote()
            return
        self._request("/v1/chat/completions",
                      {"model": self.model, "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 1}, timeout=timeout)

    def is_local(self) -> bool:
        return not self._is_cloud or is_local_url(self.host)

    def parallel_requests(self) -> int:
        # a cloud key alone isn't enough: on this PC or a LAN box the requests
        # would only queue on that one GPU
        return CLOUD_PARALLEL if self._is_cloud and not is_local_url(self.host) else 1

    def is_available(self) -> bool:
        if self._is_cloud:
            return True
        try:
            self._require_key_for_remote()
            self._request("/api/tags", timeout=3)
            return True
        except LLMError:
            return False
