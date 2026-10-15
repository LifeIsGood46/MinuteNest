"""Abstract LLM provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass


class LLMError(RuntimeError):
    """Raised when a provider call fails or is misconfigured."""

    def __init__(self, message: str = "", http_code: int | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.http_code = http_code  # lets callers distinguish 401/403 from 429 etc.
        self.retry_after = retry_after  # seconds the server asked us to wait, if any

    @property
    def is_rate_limit(self) -> bool:
        """429: the server accepted our credentials but is throttling us.

        Authentication failures are 401/403, so a 429 proves the key is valid.
        Callers use this to treat quota problems as warnings, not as bad keys.
        """
        return self.http_code == 429

    @property
    def is_quota(self) -> bool:
        """429 (throttled) or 402 (plan/credit limit): the key itself is fine."""
        return self.http_code in (402, 429)

    @property
    def is_transient(self) -> bool:
        """Worth retrying shortly: throttling or a flaky upstream."""
        # 500 / 529: Anthropic's "api_error" / "overloaded_error", retry advised
        return self.http_code in (429, 500, 502, 503, 504, 529)


class LLMEmptyAnswer(LLMError):
    """The model answered with nothing (``content: null`` or blank)."""


class LLMRefusal(LLMError):
    """The model declined the request (asking the same model again won't help)."""


def call_with_retry(fn, *, attempts: int = 3, max_wait: float = 20.0, base_wait: float = 2.0,
                    sleep=None, on_wait=None):
    """Run ``fn()``, retrying transient provider errors with backoff.

    Free tiers (OpenRouter :free models, Ollama cloud) throttle bursts
    routinely; one short wait usually clears it. Honors the server's
    Retry-After (capped by ``max_wait``). The final error is re-raised.
    ``on_wait(seconds, attempt, attempts, error)`` is told about each wait.
    """
    import time

    sleep = sleep or time.sleep
    for attempt in range(attempts):
        try:
            return fn()
        except LLMError as e:
            if not e.is_transient or attempt == attempts - 1:
                raise
            wait = e.retry_after if e.retry_after is not None else base_wait * (2 ** attempt)
            wait = min(float(wait), max_wait)
            if on_wait:
                on_wait(wait, attempt + 1, attempts, e)
            sleep(wait)
    raise LLMError("call_with_retry needs at least one attempt")


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    finish: str = ""          # why the model stopped: "stop", "length" (token limit), ...
    thinking_chars: int = 0   # reasoning the model streamed before / beside the text


# Cloud APIs answer several requests side by side. 3 keeps a long meeting well
# under OpenRouter's free-tier limit (20 requests/minute) while cutting waits ~3x.
CLOUD_PARALLEL = 3


def is_local_url(url: str) -> bool:
    """This PC or the local network: the model shares one machine's GPU."""
    import ipaddress
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if host in ("", "localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


class LLMProvider(ABC):
    """Minimal interface: one system prompt + one user message -> text."""

    provider_name: str = "base"

    def __init__(self, model: str, **kwargs):
        self.model = model
        self.options = kwargs
        self.api_key: str | None = None        # set by providers that use one
        self.status_cb: Callable[[str], None] | None = None   # waits worth showing on the card

    @abstractmethod
    def complete(self, system: str, user: str, *, max_tokens: int = 8000, watch=None) -> LLMResponse:
        """Return the model's completion for the given prompt pair.

        ``watch`` (``llm.stream.Watch``): Stop, progress callback, runaway cap and
        try number while the answer streams; None = defaults, no progress.
        """
        raise NotImplementedError

    def list_models(self) -> list[str]:
        """Model ids this provider offers to the current credentials."""
        return []

    def is_local(self) -> bool:
        """The model runs on this PC or the local network (slow first answers are normal)."""
        return False

    def limits(self):
        """How long a request may stay silent (``llm.stream.Limits``)."""
        from .stream import Limits

        return Limits.for_provider(self.is_local())

    def verify_credentials(self, timeout: int = 30) -> None:  # noqa: ARG002 - interface default
        """Best-effort credential precheck; raise LLMError if clearly invalid."""
        return

    def parallel_requests(self) -> int:
        """How many requests to send at once. A model on this PC shares one GPU:
        parallel requests only queue there and compete for VRAM, so 1."""
        return 1

    def is_available(self) -> bool:
        """Best-effort availability check (credentials, server running)."""
        return True
