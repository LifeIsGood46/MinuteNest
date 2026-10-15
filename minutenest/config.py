"""Configuration and environment setup.

Keeps all path juggling and CUDA DLL bootstrapping in one place so the rest
of the codebase never has to think about it.

Portable mode: when running from a packaged exe, EVERYTHING the app writes
(settings, jobs, whisper models, uploads, results) lives inside the exe's
own folder. Deleting the folder removes the app and all its data — no junk
scattered across the user profile.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sys
from pathlib import Path


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from .env next to the app (if present).

    Only sets variables not already defined in the environment.
    """
    env_path = Path(sys.executable).resolve().parent / ".env" if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

# ---------------------------------------------------------------------------
# Base paths
# ---------------------------------------------------------------------------

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
PROFILES_DIR = PACKAGE_DIR / "profiles"


def _app_root() -> Path:
    """The app's own folder.

    Frozen exe: the folder containing the exe (portable root — everything
    the app writes lives here). Dev checkout: the project root.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return PROJECT_ROOT


def _ensure_portable_layout() -> None:
    """Create the portable data layout next to the exe on first run."""
    if getattr(sys, "frozen", False):
        root = _app_root()
        for sub in ("data", "models", "results"):
            (root / sub).mkdir(parents=True, exist_ok=True)


_ensure_portable_layout()


def _migrate_legacy_home() -> None:
    """One-time move of pre-portable state into the portable data dir.

    Builds before full portable mode kept settings/jobs/uploads in
    ``~/.meetingnotes`` (the app's former name, MeetingNotes). On first run of a portable build, copy that state
    beside the exe so keys and job history survive the upgrade. Best-effort:
    never overwrites, never blocks boot.
    """
    legacy = Path.home() / ".meetingnotes"  # pre-rename home dir
    if not legacy.is_dir():
        return
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if any(APP_DIR.iterdir()):
            return  # already has state — don't merge, don't overwrite
        for item in legacy.iterdir():
            dest = APP_DIR / item.name
            # the old lock file and WebView2 browser cache mean nothing to a new copy
            if dest.exists() or item.suffix == ".lock" or item.name == "webview2":
                continue
            if item.is_dir():
                shutil.copytree(item, dest, ignore_dangling_symlinks=True)
            else:
                shutil.copy2(item, dest)
    except (OSError, shutil.Error) as e:   # best effort: a fresh start is still a working start
        logging.getLogger("minutenest.config").warning("could not carry over %s: %s", legacy, e)


if getattr(sys, "frozen", False):
    # ---- portable layout: everything beside the exe ----
    _root = _app_root()
    # MINUTENEST_APP_DIR moves settings/jobs/logs elsewhere (scripts, tests,
    # a second profile); the default keeps everything beside the exe.
    APP_DIR = Path(os.environ.get("MINUTENEST_APP_DIR", _root / "data"))
    MODELS_DIR = _root / "models"  # whisper downloads
    WORK_DIR = Path(os.environ.get("MINUTENEST_WORK_DIR", APP_DIR / "work"))
    OUT_DIR = Path(os.environ.get("MINUTENEST_OUT_DIR", _root / "results"))

    # HuggingFace hub cache must land in MODELS_DIR, not ~/.cache/huggingface.
    # Set before anything imports huggingface_hub.
    os.environ.setdefault("HF_HOME", str(MODELS_DIR / "hf"))
    os.environ.setdefault("HF_HUB_CACHE", str(MODELS_DIR / "hf" / "hub"))

    # The CUDA runtime JIT-compiles cuBLAS kernels on first use and caches
    # them in %APPDATA%\NVIDIA\ComputeCache (~200 MB). Point it beside the exe
    # so deleting the app folder really removes everything. Set before any
    # CUDA library loads.
    os.environ.setdefault("CUDA_CACHE_PATH", str(APP_DIR / "cuda-cache"))
    os.environ.setdefault("CUDA_CACHE_MAXSIZE", str(512 * 1024 * 1024))

    _migrate_legacy_home()
else:
    # ---- dev mode: keep project-local dirs (gitignored) ----
    APP_DIR = Path(os.environ.get("MINUTENEST_APP_DIR", PROJECT_ROOT / ".local"))
    MODELS_DIR = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    WORK_DIR = Path(os.environ.get("MINUTENEST_WORK_DIR", PROJECT_ROOT / "work"))
    OUT_DIR = Path(os.environ.get("MINUTENEST_OUT_DIR", PROJECT_ROOT / "out"))

# ---------------------------------------------------------------------------
# Model defaults
# ---------------------------------------------------------------------------

DEFAULT_MODEL_SIZE = os.environ.get("MINUTENEST_MODEL", "large-v3")
DEFAULT_DEVICE = os.environ.get("MINUTENEST_DEVICE", "cuda")
DEFAULT_COMPUTE_TYPE = os.environ.get("MINUTENEST_COMPUTE_TYPE", "float16")
DEFAULT_LANGUAGE = os.environ.get("MINUTENEST_LANGUAGE", "ru")

# ---------------------------------------------------------------------------
# CUDA DLL bootstrapping (Windows + faster-whisper)
#
# faster-whisper via CTranslate2 needs cuBLAS / CUDA runtime DLLs visible.
# On Windows they aren't on PATH by default. This scans the bundled nvidia
# packages (frozen exe) or the installed pip packages (dev) and registers
# their bin dirs.
# ---------------------------------------------------------------------------


def setup_cuda_dlls() -> None:
    """Add nvidia DLL dirs to the DLL search path (no-op elsewhere)."""
    if not sys.platform.startswith("win"):
        return

    candidates: list[Path] = []
    # 1. PyInstaller bundle (onedir: _internal sits next to the exe)
    if getattr(sys, "frozen", False):
        for base in (_app_root(), _app_root() / "_internal"):
            nvidia_root = base / "nvidia"
            if nvidia_root.is_dir():
                for pkg in nvidia_root.iterdir():
                    bin_dir = pkg / "bin"
                    if bin_dir.is_dir():
                        candidates.append(bin_dir)
        internal = _app_root() / "_internal"
        if (internal / "cublas64_12.dll").exists():
            candidates.append(internal)
        ct2 = internal / "ctranslate2"
        if ct2.is_dir():
            candidates.append(ct2)
    # 2. Regular site-packages
    for site in _site_packages():
        nvidia = site / "nvidia"
        if not nvidia.is_dir():
            continue
        for pkg in nvidia.iterdir():
            bin_dir = pkg / "bin"
            if bin_dir.is_dir():
                candidates.append(bin_dir)

    for d in candidates:
        # a folder that vanished, or Python without add_dll_directory: CUDA then just isn't found
        with contextlib.suppress(OSError, AttributeError):
            os.add_dll_directory(str(d))

    # Prepend to PATH as a fallback for older resolution logic
    if candidates:
        os.environ["PATH"] = ";".join(str(d) for d in candidates) + ";" + os.environ.get("PATH", "")


def _site_packages() -> list[Path]:
    """Return all site-packages dirs for the current interpreter."""
    paths: list[Path] = []
    for p in sys.path:
        candidate = Path(p)
        if candidate.name == "site-packages" and candidate.is_dir():
            paths.append(candidate)
    return paths


# Run once on import — cheap and idempotent.
setup_cuda_dlls()
