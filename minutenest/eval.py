"""Evaluate WER of a hypothesis against a hand-checked golden reference.

Usage:
    python -m minutenest.eval examples/golden_scada_60s.reference.txt \
                                   examples/golden_scada_60s.hypothesis.txt

Pairing convention: <name>.reference.txt / <name>.hypothesis.txt
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .wer import compute_wer


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compute WER against a golden reference")
    ap.add_argument("reference", help="Hand-checked reference transcript (.txt)")
    ap.add_argument("hypothesis", help="System output to score (.txt)")
    args = ap.parse_args(argv)

    ref = Path(args.reference).read_text(encoding="utf-8-sig")
    hyp = Path(args.hypothesis).read_text(encoding="utf-8-sig")

    r = compute_wer(ref, hyp)
    print(f"reference words : {r.reference_words}")
    print(f"hypothesis words: {r.hypothesis_words}")
    print(f"substitutions   : {r.substitutions}")
    print(f"deletions       : {r.deletions}")
    print(f"insertions      : {r.insertions}")
    print(f"WER             : {r.wer * 100:.1f}%")
    print(f"WER accuracy    : {r.accuracy * 100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
