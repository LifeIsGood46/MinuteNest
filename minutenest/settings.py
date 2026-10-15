"""Persistent application settings (JSON)."""

from __future__ import annotations

import contextlib
import copy
import functools
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any

from .config import APP_DIR

SETTINGS_PATH = APP_DIR / "settings.json"

log = logging.getLogger("minutenest.settings")

# Every read-modify-write of settings.json goes through this lock. The web
# server handles requests on a thread pool, and the UI can fire several
# settings POSTs at once (finishing the guide sends two). Without the lock a
# second writer could read a half-written file and save factory defaults over
# the user's LLM provider, key and model (real report: "No LLM is configured"
# right after the guide's sample job had used that very LLM).
_LOCK = threading.RLock()


class _Unreadable(Exception):
    """settings.json exists but cannot be parsed (truncated/corrupt)."""


def _read_raw() -> dict | None:
    """The parsed settings file, None when there is none yet.

    Retries briefly: on Windows a reader can be refused while another process
    (the CLI, a second window) is replacing the file."""
    last: Exception | None = None
    for attempt in range(5):
        if not SETTINGS_PATH.is_file():
            return None
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            last = ValueError("settings.json is not a JSON object")
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            last = e
        time.sleep(0.02 * (attempt + 1))
    raise _Unreadable(str(last))

_DEFAULTS = {
    "model_size": "large-v3",
    "device": "cuda",
    "compute_type": "float16",
    "language": "ru",
    "document_language": "",   # the LLM writes documents in it; "" = the audio language
    "default_profile": "generic",
    "default_task": "raw",
    "output_format": "both",   # md | txt | both (the LLM document / transcript)
    "output_dir": "",
    "onboarded": False,   # first-launch guide seen (legacy slides)
    "wizard_done": False, # interactive setup wizard completed/skipped
    "llm": {
        "provider": "none",
        "model": "",
        "ollama_host": "http://localhost:11434",
        "ollama_api_key": "",
        "compat_base_url": "http://localhost:1234/v1",
        "compat_api_key": "",
        "anthropic_api_key": "",
        "openai_api_key": "",
        "openrouter_api_key": "",
    },
}


@dataclass
class LLMSettings:
    provider: str = "none"
    model: str = ""
    ollama_host: str = "http://localhost:11434"
    ollama_api_key: str = ""
    compat_base_url: str = "http://localhost:1234/v1"
    compat_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openrouter_api_key: str = ""


@dataclass
class AppSettings:
    model_size: str = "large-v3"
    device: str = "cuda"
    compute_type: str = "float16"
    language: str = "ru"                  # spoken in the recordings (Whisper)
    document_language: str = ""           # LLM documents are written in it; "" = the audio language
    default_profile: str = "generic"
    default_task: str = "raw"
    output_format: str = "both"           # md | txt | both
    output_dir: str = ""                  # blank = Downloads\minutenest
    onboarded: bool = False             # first-launch guide seen (legacy slides)
    wizard_done: bool = False           # interactive setup wizard completed
    llm: LLMSettings = field(default_factory=LLMSettings)

    # -- public export (never exposes raw secrets) --------------------------
    def to_public_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for keyname in ("ollama_api_key", "compat_api_key", "anthropic_api_key", "openai_api_key",
                        "openrouter_api_key"):
            v = d["llm"][keyname]
            d["llm"][keyname] = ("*" * 8 + v[-4:]) if v else ""
        return d


@functools.lru_cache(maxsize=1)
def _has_nvidia_gpu() -> bool:
    from .transcribe import _cuda_available

    return _cuda_available()


def _fresh_settings() -> AppSettings:
    """What a new install starts with. Without an NVIDIA GPU (a VM, most
    laptops) large-v3 would run on the CPU at ~0.8x the recording's length on
    16 threads, hours on a small VM; small takes ~0.16x and is the better start."""
    s = AppSettings()
    if not _has_nvidia_gpu():
        s.model_size, s.device, s.compute_type = "small", "cpu", "int8"
    return s


def load_settings() -> AppSettings:
    with _LOCK:
        try:
            raw = _read_raw()
        except _Unreadable:
            log.exception("settings.json is unreadable; using defaults for now")
            return _fresh_settings()
    if raw is None:
        return _fresh_settings()
    return _from_raw(raw)


def _from_raw(raw: dict) -> AppSettings:

    def pick(d: dict, defaults: dict, cls):
        out = copy.copy(defaults)
        for k in defaults:
            if k in d:
                out[k] = d[k]
        return cls(**out)

    llm = pick(raw.get("llm", {}), _DEFAULTS["llm"], LLMSettings)
    scalar_keys = {k: v for k, v in _DEFAULTS.items() if k != "llm"}
    scalars = {k: raw.get(k, v) for k, v in scalar_keys.items()}
    if "onboarded" not in raw and SETTINGS_PATH.is_file():
        # Upgraded install: only returning users (with job history) skip the
        # first-launch guide. A bare settings file — e.g. migrated from a
        # legacy smoke test with no jobs — still gets the guide.
        try:
            jobs_data = json.loads((APP_DIR / "jobs.json").read_text(encoding="utf-8"))
            if isinstance(jobs_data, dict) and jobs_data.get("jobs"):
                scalars["onboarded"] = True
        except (OSError, ValueError):
            pass
    if "wizard_done" not in raw and scalars.get("onboarded"):
        # Users who saw the old slide guide are not re-nagged by the wizard.
        scalars["wizard_done"] = True
    return AppSettings(llm=llm, **scalars)


def save_settings(settings: AppSettings) -> None:
    """Write settings atomically: readers see the old file or the new one,
    never a truncated half (temp file in the same folder + os.replace)."""
    with _LOCK:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(asdict(settings), ensure_ascii=False, indent=2)
        tmp = SETTINGS_PATH.with_name(f"{SETTINGS_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for _ in range(40):
            try:
                os.replace(tmp, SETTINGS_PATH)
                return
            except PermissionError:
                # Windows: a reader (CLI, antivirus) has the file open for a
                # moment. Retry instead of losing the save.
                time.sleep(0.025)
        with contextlib.suppress(OSError):   # the leftover temp file; the next save overwrites it
            tmp.unlink()
        raise OSError(f"could not save settings to {SETTINGS_PATH}: file stayed locked")


_LOGGED_KEYS = ("model_size", "device", "compute_type", "language", "document_language", "default_profile",
                "default_task", "output_format", "output_dir", "onboarded", "wizard_done")
_LOGGED_LLM_KEYS = ("provider", "model", "ollama_host", "compat_base_url")
_SECRET_LLM_KEYS = ("ollama_api_key", "compat_api_key", "anthropic_api_key",
                    "openai_api_key", "openrouter_api_key")


def _log_changes(before: AppSettings, after: AppSettings) -> None:
    """One line per changed setting, so a log shows what changed and when.
    Keys are never logged, only whether they were set or cleared."""
    for k in _LOGGED_KEYS:
        a, b = getattr(before, k), getattr(after, k)
        if a != b:
            log.info("settings: %s %r -> %r", k, a, b)
    for k in _LOGGED_LLM_KEYS:
        a, b = getattr(before.llm, k), getattr(after.llm, k)
        if a != b:
            log.info("settings: llm.%s %r -> %r", k, a, b)
    for k in _SECRET_LLM_KEYS:
        a, b = bool(getattr(before.llm, k)), bool(getattr(after.llm, k))
        if getattr(before.llm, k) != getattr(after.llm, k):
            log.info("settings: llm.%s %s", k, "set" if b else "cleared" if a else "changed")


def update_settings(patch: dict[str, Any]) -> AppSettings:
    """Apply a partial update dict (from the UI) and persist.

    The whole read-modify-write runs under one lock, so concurrent updates
    each keep their change and never resurrect defaults."""
    with _LOCK:
        try:
            raw = _read_raw()
        except _Unreadable:
            # Keep the user's file instead of silently replacing it: it may
            # hold API keys. The app continues on defaults + this change.
            backup = SETTINGS_PATH.with_name(
                f"{SETTINGS_PATH.name}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                os.replace(SETTINGS_PATH, backup)
            except OSError:
                backup = None
            log.exception("settings.json was unreadable; kept a copy at %s and "
                          "continued with defaults", backup)
            raw = None
        before = _from_raw(raw) if raw is not None else AppSettings()
        result = _apply_patch(copy.deepcopy(before), dict(patch))
        _log_changes(before, result)
        return result


def _masked(value: Any) -> bool:
    """A masked placeholder ("********1234") the UI shows instead of a stored key."""
    return isinstance(value, str) and value.startswith("********")


def _apply_llm_patch(llm: LLMSettings, llm_patch: dict[str, Any]) -> None:
    for k, v in llm_patch.items():
        if not hasattr(llm, k) or _masked(v):   # a masked placeholder never overwrites a real key
            continue
        # Strip stray whitespace on keys/hosts from copy-paste (silent 401s)
        if isinstance(v, str) and ("api_key" in k or k.endswith(("host", "base_url"))):
            setattr(llm, k, v.strip())
        else:
            setattr(llm, k, v)


def _allowed_scalar_values() -> dict[str, tuple]:
    """Valid values of the scalar fields that drive the pipeline."""
    from .models import KNOWN_MODELS       # local imports: no cycle
    from .transcribe import LANGUAGE_NAMES

    return {
        "language": tuple(LANGUAGE_NAMES),
        "document_language": ("", *LANGUAGE_NAMES),
        "model_size": tuple(KNOWN_MODELS),
        "device": ("cuda", "cpu"),
        "compute_type": ("float16", "int8", "int8_float16", "float32"),
        "output_format": ("md", "txt", "both"),
    }


def _apply_patch(settings: AppSettings, patch: dict[str, Any]) -> AppSettings:
    _apply_llm_patch(settings.llm, patch.pop("llm", {}) or {})
    # A stale/empty form value must never be persisted: an empty model_size made
    # every job fall back to the default (large-v3) with no visible reason — the
    # real report "the model failed, I had to update models to get it working
    # again". An invalid value keeps the previous valid one.
    allowed = _allowed_scalar_values()
    for k, v in patch.items():
        if k in allowed and v not in allowed[k]:
            continue
        if hasattr(settings, k) and k != "llm":
            setattr(settings, k, v)
    # Never persist a compute type the chosen device cannot run (cpu + float16).
    from .transcribe import resolve_compute_type

    settings.compute_type = resolve_compute_type(settings.device, settings.compute_type)
    save_settings(settings)
    return settings
