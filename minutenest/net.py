"""Network helpers shared by all LLM providers.

SSL: prefer certifi's CA bundle. Python's default context reads the Windows
certificate store, which on freshly-imaged or long-offline machines can be
missing root CAs — producing CERTIFICATE_VERIFY_FAILED on perfectly healthy
networks. certifi ships the CA bundle inside the app, so cloud providers
work on any machine.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.parse
import urllib.request

_context: ssl.SSLContext | None = None


def _require_web_address(url: str) -> None:
    """Server addresses come from Settings and environment variables a user types
    in; urllib would just as happily open ``file://`` or ``ftp://`` (CWE-73,
    bandit B310), so an Ollama host of ``file:///C:/Users`` would read a folder."""
    if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
        raise ValueError(f"not a web address (http or https): {url!r}")


def http_request(url: str, *, data: bytes | None = None, headers: dict | None = None,
                 method: str | None = None) -> urllib.request.Request:
    """A request to an http(s) address; anything else is refused here."""
    _require_web_address(url)
    return urllib.request.Request(url, data=data, headers=headers or {}, method=method)  # noqa: S310  # nosec B310 - checked above


def open_url(req: urllib.request.Request, timeout: float, context: ssl.SSLContext | None = None):
    """``urllib.request.urlopen`` for http(s) addresses only."""
    _require_web_address(req.full_url)
    return urllib.request.urlopen(req, timeout=timeout, context=context)  # noqa: S310  # nosec B310 - checked above


def ssl_context() -> ssl.SSLContext:
    """Shared SSL context: certifi bundle when available, system roots else."""
    global _context
    if _context is None:
        _context = _build_context()
    return _context


def _build_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError, ssl.SSLError):   # no or unreadable bundle: Windows' own roots
        return ssl.create_default_context()


def retry_after_seconds(e: urllib.error.HTTPError) -> float | None:
    """Parse a numeric Retry-After header from an HTTP error, if present."""
    try:
        value = e.headers.get("Retry-After") if e.headers else None
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def error_detail(body: str) -> str:
    """Pull a human message out of a provider JSON error body (best effort)."""
    import json

    try:
        data = json.loads(body)
    except ValueError:         # not JSON (an HTML error page): show its start
        return body.strip()[:200]
    err = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(err, dict):
        meta = err.get("metadata") or {}
        raw = meta.get("raw") if isinstance(meta, dict) else None
        msg = err.get("message") or ""
        if raw:
            msg = f"{msg} ({str(raw)[:160]})" if msg else str(raw)[:200]
        return msg[:300]
    return str(err)[:200]


def ssl_error_hint(e: Exception) -> str | None:
    """Human-readable hint when `e` is a TLS certificate verification failure.

    Returns None for anything else, so callers can keep their normal message.
    """
    reason = e
    if isinstance(e, urllib.error.URLError) and e.reason:
        reason = e.reason
    text = str(reason).lower()
    if isinstance(reason, ssl.SSLCertVerificationError) or "certificate verify failed" in text:
        return (
            "the secure connection to the server could not be verified. "
            "Make sure the system date/time is correct, or update Windows "
            "root certificates — the app bundles its own CA list, so a "
            "reboot or an update usually fixes it."
        )
    return None

