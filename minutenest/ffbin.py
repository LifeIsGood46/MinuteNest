"""Locate ffmpeg / ffprobe binaries.

Frozen exe: the binaries ship inside the app (``_internal/ffmpeg/``), so a
clean machine needs nothing on PATH. Dev runs fall back to PATH with a
message that points at the real fix (the packaged app is self-contained).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def _frozen_dirs() -> list[Path]:
    """Candidate dirs holding the bundled ffmpeg binaries in a frozen app."""
    if not getattr(sys, "frozen", False):
        return []
    exe_dir = Path(sys.executable).resolve().parent
    return [exe_dir / "_internal" / "ffmpeg", exe_dir / "ffmpeg"]


def find_ffmpeg(name: str = "ffmpeg") -> Path | None:
    """Return the path to an ffmpeg-family binary, or None.

    Frozen apps prefer the bundled copy; only when it is missing do we
    look at PATH (dev convenience, not a packaging strategy).
    """
    exe = f"{name}.exe" if sys.platform.startswith("win") else name
    for d in _frozen_dirs():
        candidate = d / exe
        if candidate.is_file():
            return candidate
    found = shutil.which(name)
    return Path(found) if found else None


def ffmpeg_status() -> dict:
    """Presence info for the UI: bundled vs PATH vs missing."""
    bundled = _frozen_dirs()[0] / ("ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg") \
        if _frozen_dirs() else None
    return {
        "frozen": bool(getattr(sys, "frozen", False)),
        "bundled": bool(bundled and bundled.is_file()),
        "ffmpeg": bool(find_ffmpeg("ffmpeg")),
        "ffprobe": bool(find_ffmpeg("ffprobe")),
        "source": str(bundled) if (bundled and bundled.is_file()) else "PATH",
    }


class FFmpegMissing(RuntimeError):
    def __init__(self, name: str):
        if getattr(sys, "frozen", False):
            msg = (f"{name} is missing from the app package. Re-download the "
                   f"release zip — the bundled copy is incomplete.")
        else:
            msg = (f"{name} not found on PATH. Dev runs need ffmpeg installed "
                   f"(the packaged app bundles its own copy).")
        super().__init__(msg)
