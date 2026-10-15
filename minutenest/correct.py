"""Glossary-based post-correction of raw Whisper output.

Whisper mangles domain terms in predictable ways ("утилект" for "Utilitect",
"шоу месседж" for "showMessage"). A per-profile corrections map fixes these
after transcription. This is a pure, easily-tested string transform.
"""

from __future__ import annotations

import re


def apply_corrections(text: str, corrections: dict[str, str]) -> str:
    """Apply a corrections map to text.

    Matching is case-insensitive and respects word boundaries where possible.
    Corrections are applied in order of longest key first so multi-word
    phrases (e.g. "шоу месседж") take precedence over single words.
    """
    if not corrections:
        return text

    result = text
    for wrong in sorted(corrections, key=len, reverse=True):
        right = corrections[wrong]
        # Word-boundary match when the term is alphanumeric-ish; literal
        # substring otherwise (handles Cyrillic phrases with spaces).
        if re.fullmatch(r"[\w-]+", wrong, flags=re.UNICODE):
            pattern = re.compile(rf"\b{re.escape(wrong)}\b", flags=re.IGNORECASE | re.UNICODE)
        else:
            pattern = re.compile(re.escape(wrong), flags=re.IGNORECASE)
        result = pattern.sub(right, result)
    return result


def correct_segments(
    segments: list[dict], corrections: dict[str, str]
) -> list[dict]:
    """Apply corrections to a list of {start, end, text} segments.

    Returns a new list; the input is not mutated.
    """
    return [
        {**seg, "text": apply_corrections(seg.get("text", ""), corrections)}
        for seg in segments
    ]
