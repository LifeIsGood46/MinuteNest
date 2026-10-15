"""Word Error Rate (WER) evaluation.

WER = (substitutions + deletions + insertions) / reference_word_count.
Computed via a classic dynamic-programming edit distance over word tokens.
Pure stdlib — no external deps, CPU-trivial for typical transcript sizes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class WERResult:
    wer: float            # 0.0 = perfect, higher = worse (can exceed 1.0)
    substitutions: int
    deletions: int
    insertions: int
    reference_words: int
    hypothesis_words: int

    @property
    def accuracy(self) -> float:
        return max(0.0, 1.0 - self.wer)


def normalize(text: str, *, lowercase: bool = True) -> list[str]:
    """Tokenize text into words for WER comparison.

    Strips punctuation and normalizes case so only lexical content counts.
    """
    if lowercase:
        text = text.lower()
    # Keep word characters (incl. Cyrillic) and internal apostrophes/hyphens
    return re.findall(r"[\w'-]+", text, flags=re.UNICODE)


def _edit_distance(ref: list[str], hyp: list[str]) -> tuple[int, int, int, int]:
    """Return (distance, substitutions, deletions, insertions) via DP."""
    n, m = len(ref), len(hyp)
    # dp[i][j] = (dist, subs, dels, ins) aligning ref[:i] to hyp[:j]
    dp = [[(0, 0, 0, 0)] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = (i, 0, i, 0)  # all deletions
    for j in range(1, m + 1):
        dp[0][j] = (j, 0, 0, j)  # all insertions

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                sub = (dp[i - 1][j - 1][0] + 1, dp[i - 1][j - 1][1] + 1, dp[i - 1][j - 1][2], dp[i - 1][j - 1][3])
                dele = (dp[i - 1][j][0] + 1, dp[i - 1][j][1], dp[i - 1][j][2] + 1, dp[i - 1][j][3])
                ins = (dp[i][j - 1][0] + 1, dp[i][j - 1][1], dp[i][j - 1][2], dp[i][j - 1][3] + 1)
                dp[i][j] = min(sub, dele, ins, key=lambda t: t[0])

    dist, subs, dels, ins = dp[n][m]
    return dist, subs, dels, ins


def compute_wer(reference: str, hypothesis: str, *, lowercase: bool = True) -> WERResult:
    """Compute WER between reference (truth) and hypothesis (system output)."""
    ref_tokens = normalize(reference, lowercase=lowercase)
    hyp_tokens = normalize(hypothesis, lowercase=lowercase)
    if not ref_tokens:
        raise ValueError("reference must contain at least one word")

    dist, subs, dels, ins = _edit_distance(ref_tokens, hyp_tokens)
    return WERResult(
        wer=dist / len(ref_tokens),
        substitutions=subs,
        deletions=dels,
        insertions=ins,
        reference_words=len(ref_tokens),
        hypothesis_words=len(hyp_tokens),
    )
