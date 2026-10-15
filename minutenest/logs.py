"""Central debug logging: one rotating file to hand over after a test run.

Everything the backend does lands here: startup environment, window strategy,
device selection (and CPU fallbacks), job lifecycle, pipeline stages, LLM and
ffmpeg errors with tracebacks, UI-reported errors. After a test on a VM the
user can open the log from Help and send the single file.

Location: ``data/logs/minutenest.log`` beside a packaged app, or
``.local/logs/minutenest.log`` in a dev checkout. API keys are never logged.

Set ``MINUTENEST_DEBUG=1`` for DEBUG-level detail (ffmpeg commands,
per-request info). Rotates at 2 MB, keeps 3 backups.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys
import threading
import traceback
from pathlib import Path

from . import config

LOG_DIR: Path = config.APP_DIR / "logs"
LOG_PATH: Path = LOG_DIR / "minutenest.log"

_FMT = "%(asctime)s.%(msecs)03d [%(levelname)-7s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def _debug_enabled() -> bool:
    return os.environ.get("MINUTENEST_DEBUG", "").strip().lower() in ("1", "true", "yes")


def setup_logging() -> Path:
    """Configure the root logger once. Returns the log file path."""
    global _configured
    if _configured:
        return LOG_PATH
    _configured = True

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return LOG_PATH

    level = logging.DEBUG if _debug_enabled() else logging.INFO
    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)

    root = logging.getLogger()
    root.setLevel(level)
    try:
        handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(fmt)
        root.addHandler(handler)
    except OSError:
        pass  # read-only media: console only

    # Windowed exe has no stderr; console runs (dev, CLI) keep printing.
    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)

    # Third-party request chatter would drown the app's own story in the
    # handover log; keep it at WARNING unless explicitly debugging.
    third_party = "DEBUG" if _debug_enabled() else "WARNING"
    for name in ("httpx", "httpcore", "urllib3", "uvicorn", "uvicorn.access",
                 "watchfiles", "python_multipart"):
        logging.getLogger(name).setLevel(third_party)

    _install_crash_hooks()
    _startup_banner()
    return LOG_PATH


def _install_crash_hooks() -> None:
    def hook(exc_type, exc, tb):
        logging.getLogger("minutenest").critical(
            "unhandled %s: %s\n%s", exc_type.__name__, exc,
            "".join(traceback.format_exception(exc_type, exc, tb)))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        logging.getLogger("minutenest").critical(
            "unhandled in thread %s: %s\n%s", name, args.exc_value,
            "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback)))

    threading.excepthook = thread_hook


def _startup_banner() -> None:
    log = logging.getLogger("minutenest.startup")
    from . import __version__

    log.info("=" * 70)
    log.info("MinuteNest %s (frozen=%s, python=%s, %s)",
             __version__, getattr(sys, "frozen", False),
             platform.python_version(), platform.platform())
    log.info("app dir : %s", config.APP_DIR)
    log.info("out dir : %s", config.OUT_DIR)
    log.info("work dir: %s", config.WORK_DIR)
    log.info("log file: %s", LOG_PATH)
    try:
        from . import ffbin

        s = ffbin.ffmpeg_status()
        log.info("ffmpeg  : bundled=%s ffmpeg=%s ffprobe=%s source=%s",
                 s["bundled"], s["ffmpeg"], s["ffprobe"], s["source"])
    except Exception as e:  # noqa: BLE001 - a startup diagnostic must not stop the app starting
        log.warning("ffmpeg status check failed: %s", e)
    # ctranslate2 is a heavy import; only worth it where the app will use it.
    if getattr(sys, "frozen", False) or _debug_enabled():
        try:
            import ctranslate2

            log.info("cuda devices: %s", ctranslate2.get_cuda_device_count())
        except Exception as e:  # noqa: BLE001 - a missing or broken CUDA install is reported, not fatal
            log.info("cuda devices: unknown (%s)", e)


def log_path_for_ui() -> Path:
    return LOG_PATH
