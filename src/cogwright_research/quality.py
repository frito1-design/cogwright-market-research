"""Stage 5 — catalog quality signals (CW-RES-001 §7).

Computed entirely from data already retrieved in Stage 3; zero additional requests.
Shopify shops only, because the sitemap path yields URLs and nothing to inspect.

These are the observable subset of what the Hook `catalog_defects` engine surfaces
internally — enough to demonstrate the product on a cold call, nowhere near enough to
replace it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass

from .catalog import ProductRecord

THIN_TITLE_CHARS = 20

# Contributions to the 0-1 aggregate. `no_sku` leads because it is the strongest single
# indicator of absent catalog discipline; `default_variant_only` trails because plenty of
# legitimate products genuinely have one variant, so it is suggestive rather than damning.
DEFECT_WEIGHTS = {
    "pct_no_sku": 0.22,
    "pct_no_product_type": 0.18,
    "pct_no_image": 0.18,
    "pct_untagged": 0.12,
    "pct_thin_title": 0.10,
    "pct_duplicate_title": 0.08,
    "pct_zero_price": 0.07,
    "pct_default_variant_only": 0.05,
}

# How each signal reads out loud in a cold email.
PITCH_TEMPLATES = {
    "pct_no_sku": "{n:,} of {total:,} products have no SKU on any variant",
    "pct_no_product_type": "{n:,} of {total:,} products have no product type set",
    "pct_no_image": "{n:,} of {total:,} products have no image at all",
    "pct_untagged": "{n:,} of {total:,} products carry no tags",
    "pct_thin_title": "{n:,} products have titles under 20 characters",
    "pct_duplicate_title": "{n:,} products share a title with another product",
    "pct_zero_price": "{n:,} products have a variant priced at $0.00",
    "pct_default_variant_only": "{n:,} products sit on an unmanaged default variant",
}


@dataclass
class QualitySignals:
    pct_no_image: float = 0.0
    pct_no_product_type: float = 0.0
    pct_untagged: float = 0.0
    pct_default_variant_only: float = 0.0
    pct_thin_title: float = 0.0
    duplicate_title_count: int = 0
    pct_zero_price: float = 0.0
    pct_no_sku: float = 0.0
    defect_density: float = 0.0

    def as_row(self) -> dict[str, float | int]:
        return asdict(self)


def compute_quality(products: list[ProductRecord]) -> QualitySignals:
    total = len(products)
    if total == 0:
        return QualitySignals()

    titles = Counter(p.title.strip().lower() for p in products if p.title.strip())
    duplicate_titles = sum(count for count in titles.values() if count > 1)

    signals = QualitySignals(
        pct_no_image=_pct(p.image_count == 0 for p in products),
        pct_no_product_type=_pct(not p.product_type.strip() for p in products),
        pct_untagged=_pct(not p.tags.strip() for p in products),
        pct_default_variant_only=_pct(bool(p.default_title_only) for p in products),
        pct_thin_title=_pct(len(p.title.strip()) < THIN_TITLE_CHARS for p in products),
        duplicate_title_count=duplicate_titles,
        # A product counts once if any of its variants is priced at zero / has no SKU.
        pct_zero_price=_pct(p.zero_price_variants > 0 for p in products),
        pct_no_sku=_pct(p.variant_count > 0 and p.no_sku_variants == p.variant_count
                        for p in products),
    )
    signals.defect_density = round(_density(signals, total), 4)
    return signals


def _density(signals: QualitySignals, total: int) -> float:
    values = {
        "pct_duplicate_title": (signals.duplicate_title_count / total) if total else 0.0,
        **{k: getattr(signals, k) for k in DEFECT_WEIGHTS if k != "pct_duplicate_title"},
    }
    score = sum(DEFECT_WEIGHTS[k] * min(max(values[k], 0.0), 1.0) for k in DEFECT_WEIGHTS)
    return min(max(score, 0.0), 1.0)


def pitch_line(signals: QualitySignals, total_products: int) -> str:
    """The single most striking defect stat for this shop, pre-written (§11.1)."""
    if total_products == 0:
        return ""
    values = {
        "pct_duplicate_title": signals.duplicate_title_count / total_products,
        **{k: getattr(signals, k) for k in DEFECT_WEIGHTS if k != "pct_duplicate_title"},
    }
    ranked = sorted(
        values.items(), key=lambda kv: DEFECT_WEIGHTS[kv[0]] * kv[1], reverse=True
    )
    key, pct = ranked[0]
    if pct <= 0:
        return ""
    count = (
        signals.duplicate_title_count
        if key == "pct_duplicate_title"
        else round(pct * total_products)
    )
    if count <= 0:
        return ""
    return PITCH_TEMPLATES[key].format(n=count, total=total_products)


def top_defect_stats(signals: QualitySignals, total_products: int, limit: int = 5) -> list[str]:
    """The N loudest stats, for the design-partner dossiers (§11.4)."""
    if total_products == 0:
        return []
    values = {
        "pct_duplicate_title": signals.duplicate_title_count / total_products,
        **{k: getattr(signals, k) for k in DEFECT_WEIGHTS if k != "pct_duplicate_title"},
    }
    ranked = sorted(values.items(), key=lambda kv: DEFECT_WEIGHTS[kv[0]] * kv[1], reverse=True)
    out = []
    for key, pct in ranked[:limit]:
        if pct <= 0:
            continue
        count = (
            signals.duplicate_title_count
            if key == "pct_duplicate_title"
            else round(pct * total_products)
        )
        if count > 0:
            out.append(f"{PITCH_TEMPLATES[key].format(n=count, total=total_products)} ({pct:.0%})")
    return out


def _pct(flags) -> float:
    flags = list(flags)
    return round(sum(1 for f in flags if f) / len(flags), 4) if flags else 0.0
