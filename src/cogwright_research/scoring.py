"""Stage 6 — scoring and tiering (CW-RES-001 §8)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TIER_TARGETS = {
    "A": "$2,500-3,500/mo",
    "B": "$1,000-1,800/mo",
    "C": "$350-600/mo (Watchtower)",
    "D": "not a market",
}

PLATFORM_POINTS = {
    "shopify": 25,
    "bigcommerce": 18,
    "woocommerce": 14,
    "lightspeed_ecom": 12,
    "lightspeed_xseries": 12,
    "magento": 10,
    "squarespace": 5,
    "wix": 5,
    "unknown": 3,
}

DORMANT_MULTIPLIER = 0.3
LOCATOR_BONUS_THRESHOLD = 8
LOCATOR_BONUS_POINTS = 5


@dataclass
class Score:
    tier: str | None
    fit_score: float
    size_pts: float
    platform_pts: float
    vendor_pts: float
    defect_pts: float
    modifiers: dict[str, Any] = field(default_factory=dict)
    excluded: bool = False
    exclusion_reason: str | None = None


def assign_tier(catalog_size: int | None) -> str | None:
    if catalog_size is None:
        return None
    if catalog_size >= 12_000:
        return "A"
    if catalog_size >= 4_000:
        return "B"
    if catalog_size >= 1_000:
        return "C"
    return "D"


def size_points(catalog_size: int | None) -> float:
    if catalog_size is None:
        return 0.0
    if catalog_size >= 12_000:
        return 35.0
    if catalog_size >= 4_000:
        return 27.0
    if catalog_size >= 1_000:
        return 16.0
    return 3.0


def platform_points(platform: str | None) -> float:
    return float(PLATFORM_POINTS.get((platform or "unknown").lower(), 3))


def vendor_points(vendor_overlap_weighted: float | None) -> float:
    return round(min(max(vendor_overlap_weighted or 0.0, 0.0), 1.0) * 25, 2)


def defect_points(defect_density: float | None) -> float:
    """Curve peaking on the 0.35-0.65 band, with heavy decay above 0.8.

    Below the band a catalog is already tidy and there is little to sell against; above
    0.8 the catalog is not messy so much as abandoned, and abandonment is not opportunity.
    """
    if defect_density is None:
        return 0.0
    d = min(max(float(defect_density), 0.0), 1.0)
    if d < 0.15:
        return round(_lerp(d, 0.0, 0.15, 0.0, 3.0), 2)
    if d < 0.35:
        return round(_lerp(d, 0.15, 0.35, 3.0, 15.0), 2)
    if d <= 0.65:
        return 15.0
    if d <= 0.80:
        return round(_lerp(d, 0.65, 0.80, 15.0, 9.0), 2)
    return round(_lerp(d, 0.80, 1.0, 9.0, 0.0), 2)


def score_domain(
    *,
    catalog_size: int | None,
    platform: str | None,
    vendor_overlap_weighted: float | None,
    defect_density: float | None,
    dormant: bool = False,
    has_cart: bool = True,
    locator_count: int = 0,
) -> Score:
    """Combine the four components and apply the §8 modifiers."""
    tier = assign_tier(catalog_size)
    size = size_points(catalog_size)
    plat = platform_points(platform)
    vend = vendor_points(vendor_overlap_weighted)
    defect = defect_points(defect_density)

    if not has_cart:
        return Score(
            tier=tier, fit_score=0.0, size_pts=size, platform_pts=plat,
            vendor_pts=vend, defect_pts=defect,
            modifiers={"no_cart": True}, excluded=True, exclusion_reason="no_cart",
        )

    modifiers: dict[str, Any] = {}
    total = size + plat + vend + defect

    if locator_count >= LOCATOR_BONUS_THRESHOLD:
        total += LOCATOR_BONUS_POINTS
        modifiers["locator_bonus"] = LOCATOR_BONUS_POINTS
    total = min(total, 100.0)

    if dormant:
        total *= DORMANT_MULTIPLIER
        modifiers["dormant_multiplier"] = DORMANT_MULTIPLIER

    return Score(
        tier=tier,
        fit_score=round(total, 2),
        size_pts=size,
        platform_pts=plat,
        vendor_pts=vend,
        defect_pts=defect,
        modifiers=modifiers,
        excluded=tier == "D",
        exclusion_reason="tier_d_below_market" if tier == "D" else None,
    )


def _lerp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if x1 == x0:
        return y1
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
