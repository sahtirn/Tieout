"""Description similarity used by the near-match pass and the investigation tools."""

from __future__ import annotations

import re

from rapidfuzz import fuzz

# Transaction-type words that appear on one side only and carry no payee information.
STOPWORDS = frozenset(
    {
        "ach",
        "debit",
        "credit",
        "payment",
        "pmt",
        "receipt",
        "wire",
        "in",
        "out",
        "ref",
        "check",
        "chk",
        "paid",
        "deposit",
        "tkt",
        "the",
        "and",
        "of",
        "inc",
        "llc",
    }
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    tokens = _NON_ALNUM.sub(" ", text.lower()).split()
    # Numbers (check serials, traces) are compared as identifiers, never fuzzily.
    kept = [t for t in tokens if t not in STOPWORDS and not t.isdigit()]
    return " ".join(kept)


def similarity(a: str, b: str) -> float:
    """Score in [0, 1]: mean of token-set similarity and leading-token similarity.

    Banks truncate and abbreviate payee names but almost never alter the first word, so the leading
    token is weighted as half the score. Calibrated on synthetic data: legitimate pairs score at
    least 0.84, unrelated payees at most 0.74 (see docs/DECISIONS.md D-010).
    """
    ta, tb = normalize(a).split(), normalize(b).split()
    if not ta or not tb:
        return 0.0
    token_set = fuzz.token_set_ratio(" ".join(ta), " ".join(tb))
    leading = fuzz.ratio(ta[0], tb[0])
    return round((token_set + leading) / 200, 4)


def pair_similarity(
    a_desc: str,
    b_desc: str,
    a_check: str | None = None,
    b_check: str | None = None,
    a_ref: str | None = None,
    b_ref: str | None = None,
) -> tuple[float, str]:
    """Similarity with identifier overrides. Returns (score, basis)."""
    if a_check and b_check and a_check == b_check:
        return 1.0, "check_number"
    if a_ref and b_ref and a_ref == b_ref:
        return 1.0, "reference"
    return similarity(a_desc, b_desc), "description"
