"""File and folder names made from the user's file name, within Windows' path limit.

Without the long-path opt-in (``LongPathsEnabled``, off by default) a Windows
path holds at most 259 characters; anything longer fails with "No such file
or directory". Results are named after the recording, often twice
(``results/<name>/<name>.notes.md``), so a long recording name broke the job.
The user's own name is kept whenever it fits; only a name that would push a
path over the limit is shortened, at a word boundary when one is near.
"""

from __future__ import annotations

from pathlib import Path

MAX_PATH_CHARS = 259          # MAX_PATH (260) minus the terminating NUL
MIN_STEM_CHARS = 12           # never shorter than this, even in a very deep folder
LONGEST_SUFFIX = len(".testplan.txt")   # the longest ending the pipeline adds to a stem


def shorten(stem: str, limit: int) -> str:
    """``stem`` cut to at most ``limit`` characters (at least MIN_STEM_CHARS).

    Cut at the last space in the final third when there is one, and never
    ending in a space or dot (Windows drops those, so the name would change).
    """
    limit = max(MIN_STEM_CHARS, limit)
    if len(stem) <= limit:
        return stem
    cut = stem[:limit]
    space = cut.rfind(" ")
    if space >= limit * 2 // 3:
        cut = cut[:space]
    return cut.rstrip(" .-_,") or stem[:limit].rstrip(" .") or "recording"


def room_in(folder: Path, reserve: int = 0) -> int:
    """Characters left for one more name under ``folder``, keeping ``reserve`` for what follows it."""
    return MAX_PATH_CHARS - len(str(folder)) - 1 - reserve


def file_in(folder: Path, stem: str, suffix: str) -> Path:
    """``folder/<stem><suffix>``, the stem shortened only if the path would not fit."""
    return folder / f"{shorten(stem, room_in(folder, len(suffix)))}{suffix}"


def folder_stem(parent: Path, stem: str, folder_extra: int = 0, file_suffix: int = LONGEST_SUFFIX) -> str:
    """A stem short enough for ``parent/<stem + folder_extra>/<stem + file_suffix>``:
    a folder named after the recording that holds files named after it too."""
    return shorten(stem, (room_in(parent) - folder_extra - 1 - file_suffix) // 2)
