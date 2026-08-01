"""Stage 4 — vendor extraction and overlap (CW-RES-001 §6).

Matching runs exact → alias → fuzzy, and fuzzy is deliberately constrained. The Hook
vendor list contains single-word canonicals like "Peak", "Surface", "Alpine" and "Coal"
that collide with ordinary product copy; a token_set_ratio of 88 against "Peak Design"
or "Alpine Air" is trivially reached and would silently inflate every overlap figure.
Fuzzy is therefore withheld from short single-token canonicals, which must match exactly
or through a registered alias. Every fuzzy hit is recorded for manual review (§6.2).
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz, process

from .config import (
    FUZZY_MIN_CHARS,
    FUZZY_MIN_CONFIDENCE,
    FUZZY_MIN_TOKENS,
    NON_BRAND_CANONICALS,
)

CORPORATE_SUFFIXES = (
    "incorporated", "inc", "llc", "l.l.c", "ltd", "limited", "co", "company",
    "corp", "corporation", "usa", "us", "intl", "international",
)
PUNCT_RE = re.compile(r"[^\w\s&]+")
WS_RE = re.compile(r"\s+")


def normalize_vendor(name: str) -> str:
    """Fold a vendor string to a comparable key (case, punctuation, corporate suffixes)."""
    text = PUNCT_RE.sub(" ", (name or "").lower())
    tokens = [t for t in WS_RE.split(text) if t]
    while tokens and tokens[-1] in CORPORATE_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


@dataclass
class VendorMatch:
    vendor_raw: str
    vendor_canonical: str | None
    match_type: str  # exact | alias | fuzzy | none
    confidence: float
    product_count: int = 0

    @property
    def matched(self) -> bool:
        return self.vendor_canonical is not None


@dataclass
class OverlapReport:
    matches: list[VendorMatch] = field(default_factory=list)
    vendor_overlap_pct: float = 0.0
    vendor_overlap_weighted: float = 0.0
    unmatched_vendors: list[str] = field(default_factory=list)
    fuzzy_review: list[VendorMatch] = field(default_factory=list)


class VendorLibrary:
    """The Hook vendor reference (canonical names + aliases), loaded from CSV."""

    def __init__(self, rows: Iterable[tuple[str, str, bool]]) -> None:
        self.canonicals: dict[str, str] = {}  # normalized canonical -> display name
        self.lookup: dict[str, tuple[str, str]] = {}  # normalized key -> (display, kind)
        for canonical, alias, is_primary in rows:
            if canonical.strip().lower() in NON_BRAND_CANONICALS:
                continue
            key_canonical = normalize_vendor(canonical)
            if not key_canonical:
                continue
            self.canonicals.setdefault(key_canonical, canonical)
            key_alias = normalize_vendor(alias)
            if not key_alias:
                continue
            kind = "exact" if is_primary or key_alias == key_canonical else "alias"
            # An exact canonical always outranks an alias pointing elsewhere.
            if key_alias not in self.lookup or kind == "exact":
                self.lookup[key_alias] = (canonical, kind)

        self._fuzzy_pool = {
            key: display
            for key, display in self.canonicals.items()
            if _fuzzy_eligible(key)
        }
        self._fuzzy_keys = list(self._fuzzy_pool)

    @classmethod
    def from_csv(cls, path: Path) -> VendorLibrary:
        with Path(path).open(encoding="utf-8") as fh:
            rows = [
                (r["canonical_name"], r["alias"], str(r["is_primary"]).lower() == "true")
                for r in csv.DictReader(fh)
            ]
        return cls(rows)

    def match(self, raw: str) -> VendorMatch:
        key = normalize_vendor(raw)
        if not key:
            return VendorMatch(raw, None, "none", 0.0)

        hit = self.lookup.get(key)
        if hit is not None:
            display, kind = hit
            return VendorMatch(raw, display, kind, 100.0)

        if not self._fuzzy_keys or not _fuzzy_eligible(key):
            return VendorMatch(raw, None, "none", 0.0)

        best = process.extractOne(
            key, self._fuzzy_keys, scorer=fuzz.token_set_ratio, score_cutoff=FUZZY_MIN_CONFIDENCE
        )
        if best is None:
            return VendorMatch(raw, None, "none", 0.0)
        matched_key, score, _ = best
        return VendorMatch(raw, self._fuzzy_pool[matched_key], "fuzzy", float(score))

    def analyze(self, vendor_counts: Counter[str] | dict[str, int]) -> OverlapReport:
        """Score one shop's vendor mix against the library."""
        counts = Counter(vendor_counts)
        matches = [
            _with_count(self.match(vendor), count)
            for vendor, count in counts.items()
            if vendor.strip()
        ]
        if not matches:
            return OverlapReport()

        total_products = sum(m.product_count for m in matches) or 1
        matched = [m for m in matches if m.matched]
        return OverlapReport(
            matches=sorted(matches, key=lambda m: -m.product_count),
            vendor_overlap_pct=round(len(matched) / len(matches), 4),
            vendor_overlap_weighted=round(sum(m.product_count for m in matched) / total_products, 4),
            unmatched_vendors=sorted(m.vendor_raw for m in matches if not m.matched),
            fuzzy_review=[m for m in matched if m.match_type == "fuzzy"],
        )


def _with_count(match: VendorMatch, count: int) -> VendorMatch:
    match.product_count = count
    return match


def _fuzzy_eligible(normalized_key: str) -> bool:
    """Short single-word names are too collision-prone to fuzzy match."""
    tokens = normalized_key.split()
    return len(tokens) >= FUZZY_MIN_TOKENS or len(normalized_key) >= FUZZY_MIN_CHARS
