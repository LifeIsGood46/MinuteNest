"""Domain profiles: vocabulary prompts + glossary corrections per meeting type."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROFILES_DIR

log = logging.getLogger("minutenest.profiles")
# what a broken profile file raises: unreadable, not JSON, or not a JSON object
_BAD_FILE = (OSError, ValueError, KeyError, TypeError, AttributeError)


@dataclass
class Profile:
    name: str
    description: str = ""
    language: str = "ru"
    initial_prompt: str = ""
    corrections: dict[str, str] = field(default_factory=dict)
    model: str = ""  # optional per-profile whisper model preset (e.g. "tiny")


def _split_stem(stem: str) -> tuple[str, str | None]:
    """Split 'software.en' -> ('software', 'en'); 'software' -> ('software', None)."""
    parts = stem.rsplit(".", 1)
    if len(parts) == 2 and 2 <= len(parts[1]) <= 3 and parts[1].isalpha():
        return parts[0], parts[1].lower()
    return stem, None


def _read_profile_file(path: Path, fallback_name: str) -> Profile:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Profile(
        name=data.get("name", fallback_name),
        description=data.get("description", ""),
        language=data.get("language", "ru"),
        initial_prompt=data.get("initial_prompt", ""),
        corrections=data.get("corrections", {}),
        model=data.get("model", ""),
    )


def _candidate_files(name: str, lang: str | None) -> list[Path]:
    """Ordered fallback for one profile name.

    Exact language variant first; for languages without their own dicts,
    English acts as the lingua franca (better shared vocabulary than the
    RU base); the unsuffixed (RU) file comes last before generic.
    """
    out: list[Path] = []
    if lang:
        out.append(PROFILES_DIR / f"{name}.{lang}.json")
    if lang and lang not in ("ru", "en"):
        out.append(PROFILES_DIR / f"{name}.en.json")
    out.append(PROFILES_DIR / f"{name}.json")
    return out


def load_profile(name: str, language: str | None = None) -> Profile:
    """Load a profile by name, preferring the transcription-language variant.

    Fallback chain: ``{name}.{lang}.json`` -> ``{name}.en.json`` (non-ru/en
    languages only) -> ``{name}.json`` -> same chain for ``generic``.
    """
    lang = (language or "").strip().lower() or None
    for candidate in dict.fromkeys([name, "generic"]):    # the profile, else generic (no recursion)
        for path in _candidate_files(candidate, lang):
            if not path.is_file():
                continue
            try:
                return _read_profile_file(path, candidate)
            except _BAD_FILE as e:
                log.warning("profile file %s is unreadable, trying the next one: %s", path, e)
    raise FileNotFoundError(f"Profile not found: {name}")


def list_profiles() -> list[Profile]:
    """Return all available profiles — one entry per profile name.

    Language variants (``{name}.{lang}.json``) share one dropdown entry;
    the variant is picked at transcription time from the audio language.
    """
    by_name: dict[str, Profile] = {}
    order: list[str] = []
    for path in sorted(PROFILES_DIR.glob("*.json")):
        name, lang = _split_stem(path.stem)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            prof_name = data.get("name", name)
        except _BAD_FILE as e:
            log.warning("profile file %s is unreadable, left out of the list: %s", path, e)
            continue
        if prof_name not in by_name:
            order.append(prof_name)
        # Prefer the default (unsuffixed) variant for display.
        if prof_name not in by_name or lang is None:
            try:
                by_name[prof_name] = _read_profile_file(path, name)
            except _BAD_FILE as e:
                log.warning("profile file %s is unreadable, left out of the list: %s", path, e)
    return [by_name[k] for k in order]
