"""OpenRouter provider (OpenAI-compatible API, incl. :free models and the free router)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from ..net import error_detail, http_request, open_url, retry_after_seconds, ssl_context, ssl_error_hint
from .base import CLOUD_PARALLEL, LLMProvider, LLMResponse, LLMError, call_with_retry
from .stream import Watch, post_streamed, read_openai_sse

log = logging.getLogger("minutenest.llm.openrouter")

API_BASE = "https://openrouter.ai/api/v1"
ROUTER = "openrouter/free"   # OpenRouter's Free Models Router: a random free model per request

# Free models share one pool and are regularly "rate-limited upstream". For a
# :free choice, OpenRouter's `models` list lets it fall back to these, in order;
# it reports which model answered and bills that one, and every entry is :free,
# so the cost stays $0. Only ids present in the live catalog are sent: one stale
# id makes OpenRouter reject the whole request. These are also where the free
# router's later tries go: measured 2026-10-13 on a real meeting, the router's
# own picks wrote a usable document 4 times in 12 (reasoning-only answers with no
# text, a safety classifier, runaways); of its picks only nemotron-3-ultra wrote.
FREE_FALLBACKS = (
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-26b-a4b-it:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
)
MAX_MODELS = 3  # OpenRouter answers 400 to a longer `models` list
ROUTER_OWN_TRIES = 2  # tries left to the router's random pick before the list above takes over
# Free-pool throttling clears in minutes, not seconds: wait 8+16+32+60 s
# (about two minutes) before failing. Paid models keep the short default.
FREE_RETRY = {"attempts": 5, "base_wait": 8.0, "max_wait": 60.0}
_CATALOG_TTL_S = 600.0
_catalog: tuple[float, frozenset[str]] | None = None
_catalog_lock = threading.Lock()


def api_base() -> str:
    """OPENROUTER_BASE_URL redirects the API (a proxy or gateway; tests), as for OpenAI."""
    return (os.environ.get("OPENROUTER_BASE_URL") or API_BASE).rstrip("/")


def is_free(model_id: str) -> bool:
    """A model (or the router) that only ever runs free models."""
    return model_id.endswith(":free") or model_id == ROUTER


def _live_free_models(key: str | None = None) -> frozenset[str]:
    """Free model ids in OpenRouter's catalog right now (cached; empty if unreachable)."""
    global _catalog
    with _catalog_lock:
        if _catalog and time.monotonic() - _catalog[0] < _CATALOG_TTL_S:
            return _catalog[1]
    try:
        ids = OpenRouterProvider(model="", api_key=key).list_models(free_only=True)
    except LLMError as e:
        log.warning("OpenRouter catalog unavailable, no free fallbacks this time: %s", e)
        return frozenset()
    with _catalog_lock:
        _catalog = (time.monotonic(), frozenset(ids))
        return _catalog[1]


def is_classifier_model(model_id: str) -> bool:
    """Moderation/safety models: they classify text, they don't write documents."""
    m = model_id.lower()
    return any(w in m for w in ("guard", "safeguard", "content-safety", "moderation", "shield"))


class OpenRouterProvider(LLMProvider):
    provider_name = "openrouter"

    def __init__(self, model: str, api_key: str | None = None, **kwargs):
        super().__init__(model, **kwargs)
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.api_key = key.strip() if key else None
        self.models_used: list[str] = []  # which model answered each call
        self.status_cb = None  # callable(str): shows waits on the job card

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/LifeIsGood46/MinuteNest",
            "X-Title": "minutenest",
        }

    def _http_error(self, e: urllib.error.HTTPError) -> LLMError:
        body = e.read().decode("utf-8", "replace")
        if e.code in (401, 403):
            return LLMError("Invalid or expired OpenRouter API key. Open Settings, paste a fresh key.",
                            http_code=e.code)
        if e.code == 402:
            return LLMError("OpenRouter: insufficient credits. Pick a :free model or top up your key.",
                            http_code=e.code)
        if e.code == 429:
            detail = error_detail(body)
            return LLMError(
                "OpenRouter is throttling this model right now (your key is fine). "
                "Free models share a pool and are often busy: wait a minute, pick "
                "another :free model, or use a paid one." + (f" Details: {detail}" if detail else ""),
                http_code=e.code, retry_after=retry_after_seconds(e))
        return LLMError(f"OpenRouter request failed ({e.code}): {error_detail(body)}",
                        http_code=e.code, retry_after=retry_after_seconds(e))

    @staticmethod
    def _conn_error(e: Exception) -> LLMError:
        hint = ssl_error_hint(e)
        if hint:
            return LLMError(f"OpenRouter connection failed: {hint}")
        return LLMError(f"Could not reach OpenRouter: {e}.")

    def _request(self, path: str, payload: dict | None = None, timeout: int = 600) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = http_request(f"{api_base()}{path}", data=data, headers=self._headers(),
                                     method="POST" if payload is not None else "GET")
        try:
            with open_url(req, timeout=timeout, context=ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise self._http_error(e) from e
        except Exception as e:
            raise self._conn_error(e) from e

    def list_models(self, *, free_only: bool = False) -> list[str]:
        ids = [m.get("id", "") for m in self._request("/models", timeout=15).get("data", [])]
        # Safety classifiers (llama-guard, nemotron content-safety, ...) answer
        # "User Safety: safe" instead of writing text; they can't do this job.
        ids = [i for i in ids if i and not is_classifier_model(i)]
        if free_only:
            ids = [i for i in ids if i.endswith(":free")]
        return sorted(set(ids))

    def verify_credentials(self, timeout: int = 30) -> None:
        """Check the key against /auth/key.

        That endpoint answers 401 for a bad key and consumes no model quota,
        so it cannot hit the per-model throttling that free models suffer
        (the old 1-token chat probe reported "rate limit" for perfectly good
        keys whenever the shared free pool was busy).
        """
        if not self.api_key:
            raise LLMError("OpenRouter API key is not set. Open Settings and paste your key.")
        self._request("/auth/key", timeout=timeout)

    def _models_for(self, attempt: int) -> list[str]:
        """Which model(s) one try asks for, in order (OpenRouter's `models` fallback list).

        The router keeps its own random pick for its first tries; after that the
        list of free models that write documents takes over (the router can't be
        told to skip reasoning-only models or safety classifiers).
        """
        if self.model == ROUTER:
            if attempt <= ROUTER_OWN_TRIES:
                return [ROUTER]
            live = _live_free_models(self.api_key)
            picks = [m for m in FREE_FALLBACKS if m in live]
            return picks[:MAX_MODELS] or [ROUTER]
        if self.model.endswith(":free"):
            live = _live_free_models(self.api_key)
            return [self.model, *[m for m in FREE_FALLBACKS if m != self.model and m in live]][:MAX_MODELS]
        return [self.model]

    def complete(self, system: str, user: str, *, max_tokens: int = 8000,
                 watch: Watch | None = None) -> LLMResponse:
        if not self.api_key:
            raise LLMError("OpenRouter API key is not set. Open Settings and paste your key.")
        watch = watch or Watch()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        models = self._models_for(watch.attempt)
        retry = FREE_RETRY if is_free(self.model) else {}

        def send(chosen: list[str]) -> LLMResponse:
            payload: dict = {"messages": messages, "max_tokens": max_tokens, "stream": True}
            if len(chosen) > 1:
                payload["models"] = chosen
            else:
                payload["model"] = chosen[0]
            return post_streamed(f"{api_base()}/chat/completions", self._headers(), payload, read_openai_sse,
                                 provider=self, model=chosen[0], watch=watch,
                                 http_error=self._http_error, conn_error=self._conn_error)

        try:
            answer = call_with_retry(lambda: send(models), on_wait=self._on_wait, **retry)
        except LLMError as e:
            if not (len(models) > 1 and e.http_code == 400):
                raise
            # A fallback left the catalog since we listed it. The safety net
            # must never break the user's own choice: ask for the first one alone.
            log.warning("OpenRouter rejected the fallback list (%s); retrying %s alone", e, models[0])
            answer = call_with_retry(lambda: send(models[:1]), on_wait=self._on_wait, **retry)
        if answer.model != self.model:
            log.info("OpenRouter: %s was answered by %s", self.model, answer.model)
        self.models_used.append(answer.model)
        return answer

    def _on_wait(self, seconds: float, attempt: int, attempts: int, err: LLMError) -> None:
        log.warning("OpenRouter busy (%s); waiting %.0f s before try %d of %d",
                    err.http_code, seconds, attempt + 1, attempts)
        if self.status_cb:
            self.status_cb(f"OpenRouter's free models are busy, trying again in "
                           f"{seconds:.0f} s (try {attempt + 1} of {attempts})")

    def is_available(self) -> bool:
        return bool(self.api_key)

    def parallel_requests(self) -> int:
        return CLOUD_PARALLEL
