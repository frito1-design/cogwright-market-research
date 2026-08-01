from collections import Counter

from cogwright_research.catalog import ProductRecord
from cogwright_research.quality import compute_quality, pitch_line, top_defect_stats
from cogwright_research.scoring import (
    assign_tier,
    defect_points,
    platform_points,
    score_domain,
    vendor_points,
)
from cogwright_research.vendors_match import VendorLibrary, normalize_vendor

ROWS = [
    ("Simms", "Simms", True),
    ("Simms", "simms fishing products llc", False),
    ("Fishpond", "Fishpond", True),
    ("Fishpond", "fishpond usa", False),
    ("Scientific Anglers", "Scientific Anglers", True),
    ("Temple Fork Outfitters", "Temple Fork Outfitters", True),
    ("Temple Fork Outfitters", "tfo", False),
    ("Peak", "Peak", True),
    ("amazon", "amazon", True),  # dropped as a non-brand bookkeeping row
]


def library() -> VendorLibrary:
    return VendorLibrary(ROWS)


def test_normalize_vendor_folds_case_punctuation_and_corporate_suffixes():
    assert normalize_vendor("Simms Fishing Products, LLC.") == "simms fishing products"
    assert normalize_vendor("Hareline Dubbin, LLC.") == "hareline dubbin"
    assert normalize_vendor("  ") == ""


def test_exact_and_alias_matching():
    lib = library()
    assert lib.match("Simms").match_type == "exact"
    assert lib.match("SIMMS").vendor_canonical == "Simms"
    alias = lib.match("Simms Fishing Products LLC")
    assert alias.vendor_canonical == "Simms"
    assert alias.confidence == 100.0
    assert lib.match("TFO").vendor_canonical == "Temple Fork Outfitters"


def test_non_brand_rows_are_excluded_from_the_library():
    assert library().match("amazon").matched is False


def test_fuzzy_matching_applies_to_multiword_names():
    result = library().match("Scientific Anglers Inc")
    assert result.vendor_canonical == "Scientific Anglers"
    assert result.match_type in {"alias", "exact", "fuzzy"}


def test_short_single_word_canonicals_never_fuzzy_match():
    """"Peak Design" must not be folded into the vendor "Peak" and inflate overlap."""
    result = library().match("Peak Design")
    assert result.vendor_canonical is None
    assert result.match_type == "none"


def test_unrelated_vendors_do_not_match():
    assert library().match("Yeti Coolers").matched is False


def test_overlap_is_computed_plain_and_weighted():
    report = library().analyze(Counter({"Simms": 400, "Fishpond": 100, "Yeti": 500}))
    assert report.vendor_overlap_pct == round(2 / 3, 4)
    assert report.vendor_overlap_weighted == round(500 / 1000, 4)
    assert report.unmatched_vendors == ["Yeti"]


def test_fuzzy_matches_are_surfaced_for_review():
    report = library().analyze(Counter({"Scientific Anglers Corp": 10}))
    for match in report.matches:
        if match.match_type == "fuzzy":
            assert match in report.fuzzy_review
            assert match.confidence >= 88


# --------------------------------------------------------------------------------------


def products(**kwargs) -> list[ProductRecord]:
    defaults = {
        "product_id": "1", "title": "A perfectly good product title", "vendor": "Simms",
        "product_type": "Waders", "tags": "a, b", "variant_count": 2, "image_count": 2,
        "min_price": 10.0, "published_at": None, "updated_at": None,
    }
    defaults.update(kwargs)
    return [ProductRecord(**defaults)]


def test_quality_signals_on_a_clean_catalog():
    signals = compute_quality(products() * 1)
    assert signals.pct_no_image == 0.0
    assert signals.pct_no_sku == 0.0
    assert signals.defect_density == 0.0


def test_quality_signals_on_a_broken_catalog():
    broken = [
        ProductRecord("1", "Fly", "", "", "", 1, 0, 0.0, None, None,
                      no_sku_variants=1, default_title_only=1, zero_price_variants=1),
        ProductRecord("2", "Fly", "", "", "", 1, 0, 0.0, None, None,
                      no_sku_variants=1, default_title_only=1, zero_price_variants=1),
    ]
    signals = compute_quality(broken)
    assert signals.pct_no_image == 1.0
    assert signals.pct_no_product_type == 1.0
    assert signals.pct_untagged == 1.0
    assert signals.pct_no_sku == 1.0
    assert signals.pct_thin_title == 1.0
    assert signals.duplicate_title_count == 2  # both share the title "Fly"
    assert signals.defect_density > 0.9


def test_empty_catalog_yields_zeroed_signals():
    signals = compute_quality([])
    assert signals.defect_density == 0.0
    assert pitch_line(signals, 0) == ""


def test_pitch_line_quotes_absolute_numbers():
    signals = compute_quality(
        [ProductRecord(str(i), "Some Reasonable Title Here", "V", "", "t", 1, 1, 5.0,
                       None, None, no_sku_variants=1) for i in range(100)]
    )
    line = pitch_line(signals, 100)
    assert "100 of 100 products have no SKU" in line


def test_top_defect_stats_are_ranked_and_capped():
    signals = compute_quality(
        [ProductRecord(str(i), "X", "V", "", "", 1, 0, 0.0, None, None,
                       no_sku_variants=1, zero_price_variants=1) for i in range(50)]
    )
    stats = top_defect_stats(signals, 50, limit=5)
    assert 1 <= len(stats) <= 5
    assert "no SKU" in stats[0]


# --------------------------------------------------------------------------------------


def test_tiers_follow_the_sku_bands():
    assert assign_tier(20_000) == "A"
    assert assign_tier(12_000) == "A"
    assert assign_tier(11_999) == "B"
    assert assign_tier(4_000) == "B"
    assert assign_tier(3_999) == "C"
    assert assign_tier(1_000) == "C"
    assert assign_tier(999) == "D"
    assert assign_tier(None) is None


def test_component_points_match_the_weight_table():
    assert platform_points("shopify") == 25
    assert platform_points("woocommerce") == 14
    assert platform_points("wix") == 5
    assert platform_points(None) == 3
    assert vendor_points(1.0) == 25
    assert vendor_points(0.4) == 10
    assert vendor_points(None) == 0


def test_defect_curve_peaks_on_the_band_and_decays_above_it():
    assert defect_points(0.5) == 15.0
    assert defect_points(0.35) == 15.0
    assert defect_points(0.65) == 15.0
    assert defect_points(0.05) < 3
    assert defect_points(0.9) < defect_points(0.75)
    assert defect_points(1.0) == 0.0
    assert defect_points(None) == 0.0


def test_perfect_prospect_scores_near_the_ceiling():
    score = score_domain(
        catalog_size=15_000, platform="shopify", vendor_overlap_weighted=1.0,
        defect_density=0.5, locator_count=10,
    )
    assert score.tier == "A"
    assert score.fit_score == 100.0          # 35+25+25+15 = 100, +5 bonus, clamped
    assert score.modifiers["locator_bonus"] == 5


def test_dormant_shops_are_crushed():
    live = score_domain(catalog_size=15_000, platform="shopify",
                        vendor_overlap_weighted=0.8, defect_density=0.5)
    dormant = score_domain(catalog_size=15_000, platform="shopify",
                           vendor_overlap_weighted=0.8, defect_density=0.5, dormant=True)
    assert dormant.fit_score == round(live.fit_score * 0.3, 2)
    assert dormant.modifiers["dormant_multiplier"] == 0.3


def test_no_cart_is_excluded_outright():
    score = score_domain(catalog_size=15_000, platform="shopify",
                         vendor_overlap_weighted=1.0, defect_density=0.5, has_cart=False)
    assert score.excluded is True
    assert score.exclusion_reason == "no_cart"
    assert score.fit_score == 0.0


def test_tier_d_is_flagged_out_of_market():
    score = score_domain(catalog_size=200, platform="shopify",
                         vendor_overlap_weighted=1.0, defect_density=0.5)
    assert score.tier == "D"
    assert score.excluded is True
