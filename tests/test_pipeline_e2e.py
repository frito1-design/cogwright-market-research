"""End-to-end: fake storefronts in, four deliverables out.

This is the closest thing to a real run that exists without network egress. It exercises
every stage — robots, platform detection, both catalog paths, vendor overlap, quality
signals, scoring, persistence and all four exports — against synthetic shops whose
expected numbers are known exactly.
"""

import json
import sqlite3

import httpx
import pandas as pd
import pytest

from cogwright_research import db
from cogwright_research.config import CrawlPolicy
from cogwright_research.exporters import export_all
from cogwright_research.fetcher import PoliteFetcher
from cogwright_research.pipeline import crawl_one, persist, run_crawl
from cogwright_research.vendors_match import VendorLibrary

VENDOR_ROWS = [
    ("Simms", "Simms", True),
    ("Fishpond", "Fishpond", True),
    ("Fishpond", "fishpond usa", False),
    ("Scientific Anglers", "Scientific Anglers", True),
    ("Hareline", "Hareline", True),
    ("Hareline", "hareline dubbin", False),
]

SHOPIFY_HOME = """<html><head><title>TCO Fly Shop</title>
<script src="https://cdn.shopify.com/s/files/1/theme.js"></script></head>
<body><button>Add to Cart</button></body></html>"""

WOO_HOME = """<html><head><title>Driftless Angler</title>
<link rel="stylesheet" href="/wp-content/plugins/woocommerce/assets/css/woocommerce.css">
</head><body><a href="/cart">Cart</a></body></html>"""

SITEMAP_INDEX = """<?xml version="1.0"?><sitemapindex>
<sitemap><loc>https://driftlessangler.com/sitemap-products-1.xml</loc></sitemap>
</sitemapindex>"""


def product(i: int, *, vendor: str, broken: bool) -> dict:
    if broken:
        return {
            "id": i, "title": "Fly", "vendor": vendor, "product_type": "", "tags": "",
            "published_at": "2025-01-01T00:00:00Z", "updated_at": "2026-07-20T00:00:00Z",
            "variants": [{"title": "Default Title", "price": "0.00", "sku": ""}],
            "images": [],
        }
    return {
        "id": i, "title": f"Simms Freestone Wading Boot Size {i}", "vendor": vendor,
        "product_type": "Boots", "tags": ["boots", "simms"],
        "published_at": "2025-01-01T00:00:00Z", "updated_at": "2026-07-20T00:00:00Z",
        "variants": [{"title": "10", "price": "199.95", "sku": f"SKU-{i}"}],
        "images": [{"src": "a.jpg"}],
    }


def make_handler() -> httpx.MockTransport:
    """Three shops: a big messy Shopify, a small clean Shopify, and a Woo on sitemaps."""
    big = [product(i, vendor="Simms" if i % 2 else "Yeti", broken=i % 3 != 0)
           for i in range(1, 1201)]
    small = [product(i, vendor="Fishpond", broken=False) for i in range(1, 41)]

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host.replace("www.", "")
        path = request.url.path
        page = int(request.url.params.get("page", 1) or 1)

        if path == "/robots.txt":
            if host == "blocked.com":
                return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
            return httpx.Response(200, text="User-agent: *\nDisallow: /admin\n")

        if host == "tcoflyfishing.com":
            if path == "/":
                return httpx.Response(200, text=SHOPIFY_HOME,
                                      headers={"content-type": "text/html"})
            if path == "/products.json":
                chunk = big[(page - 1) * 250: page * 250]
                return httpx.Response(200, json={"products": chunk})

        if host == "smallshop.com":
            if path == "/":
                return httpx.Response(200, text=SHOPIFY_HOME,
                                      headers={"content-type": "text/html"})
            if path == "/products.json":
                chunk = small[(page - 1) * 250: page * 250]
                return httpx.Response(200, json={"products": chunk})

        if host == "driftlessangler.com":
            if path == "/":
                return httpx.Response(200, text=WOO_HOME,
                                      headers={"content-type": "text/html"})
            if path == "/sitemap.xml":
                return httpx.Response(200, text=SITEMAP_INDEX,
                                      headers={"content-type": "application/xml"})
            if path == "/sitemap-products-1.xml":
                locs = "".join(
                    f"<url><loc>https://driftlessangler.com/product/item-{i}</loc>"
                    f"<lastmod>2026-07-01</lastmod></url>"
                    for i in range(1, 1502)
                )
                return httpx.Response(200, text=f"<urlset>{locs}</urlset>",
                                      headers={"content-type": "application/xml"})

        if host == "blocked.com":
            return httpx.Response(200, text="<html>never reached</html>")

        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fetcher() -> PoliteFetcher:
    clock = Clock()
    return PoliteFetcher(
        CrawlPolicy(), transport=make_handler(), clock=clock, sleep=clock.sleep
    )


@pytest.fixture
def library() -> VendorLibrary:
    return VendorLibrary(VENDOR_ROWS)


async def test_large_messy_shopify_shop(fetcher, library):
    outcome = await crawl_one(fetcher, "tcoflyfishing.com", library, locator_count=9)

    assert outcome.platform == "shopify"
    assert outcome.has_cart is True
    assert outcome.catalog.catalog_method == "shopify_products_json"
    assert outcome.catalog.catalog_size == 1200
    assert outcome.catalog.variant_count == 1200
    assert outcome.catalog.dormant is False

    # Two of every three products are broken.
    assert outcome.quality.pct_no_sku == pytest.approx(800 / 1200, abs=0.01)
    assert outcome.quality.defect_density > 0.4

    # Simms is in the library, Yeti is not, and they split the catalog evenly.
    assert outcome.overlap.unmatched_vendors == ["Yeti"]
    assert outcome.overlap.vendor_overlap_pct == 0.5
    assert 0.45 < outcome.overlap.vendor_overlap_weighted < 0.55

    assert outcome.score is not None
    assert outcome.score.tier == "C"           # 1,200 SKUs
    assert outcome.score.platform_pts == 25
    assert outcome.score.modifiers.get("locator_bonus") == 5


async def test_small_clean_shop_is_tier_d_and_scores_low(fetcher, library):
    outcome = await crawl_one(fetcher, "smallshop.com", library)

    assert outcome.catalog.catalog_size == 40
    assert outcome.quality.defect_density == 0.0
    assert outcome.overlap.vendor_overlap_weighted == 1.0
    assert outcome.score.tier == "D"
    assert outcome.score.excluded is True


async def test_woocommerce_falls_back_to_sitemap_counting(fetcher, library):
    outcome = await crawl_one(fetcher, "driftlessangler.com", library)

    assert outcome.platform == "woocommerce"
    assert outcome.catalog.catalog_method == "sitemap"
    assert outcome.catalog.catalog_size == 1501
    assert outcome.catalog.variant_count is None
    # §7 signals need product records, which the sitemap path cannot provide.
    assert outcome.quality.defect_density == 0.0
    assert outcome.overlap.matches == []
    assert outcome.score.tier == "C"
    assert outcome.score.platform_pts == 14


async def test_a_fully_disallowed_site_is_recorded_not_crawled(fetcher, library):
    outcome = await crawl_one(fetcher, "blocked.com", library)

    assert outcome.robots_blocked is True
    assert outcome.error == "homepage_unavailable"
    assert outcome.catalog.catalog_size is None
    assert fetcher.violations() == []


async def test_full_run_persists_scores_and_writes_all_four_exports(tmp_path, library):
    clock = Clock()
    fetcher = PoliteFetcher(
        CrawlPolicy(), transport=make_handler(), clock=clock, sleep=clock.sleep
    )

    class TestPaths:
        db = tmp_path / "market.db"
        exports = tmp_path / "exports"

    targets = [
        ("tcoflyfishing.com", 9),
        ("smallshop.com", 1),
        ("driftlessangler.com", 2),
    ]
    with db.connect(TestPaths.db) as conn:
        for domain, count in targets:
            db.upsert_domain(conn, {
                "domain": domain, "root_url": f"https://{domain}",
                "first_seen": "2026-08-01",
                "source_locators": json.dumps([f"locator:{i}" for i in range(count)]),
                "excluded": 0, "exclusion_reason": None,
            })

    outcomes = await run_crawl(targets, library, paths=TestPaths, fetcher=fetcher)
    await fetcher.aclose()
    assert len(outcomes) == 3

    written = export_all(TestPaths.db, TestPaths.exports)
    for path in written.values():
        assert path.exists() and path.stat().st_size > 0

    frame = pd.read_excel(written["prospects"])
    assert len(frame) == 3
    assert list(frame["fit_score"]) == sorted(frame["fit_score"], reverse=True)
    assert set(frame["pos_platform"]) == {"unknown"}          # §4
    top = frame.iloc[0]
    assert top["domain"] == "tcoflyfishing.com"
    assert "products" in str(top["pitch_line"])

    summary = written["summary"].read_text(encoding="utf-8")
    assert "Measured SAM" in summary
    assert "robots.txt violations: **0**" in summary
    assert "not evidence that" in summary                     # the POS caveat survives

    gap = pd.read_csv(written["vendor_gap"])
    assert "Yeti" in set(gap["vendor_raw"])

    dossiers = written["dossiers"].read_text(encoding="utf-8")
    assert "TCO Fly Shop" in dossiers
    assert "Schultz Outfitters" in dossiers                   # absent -> says so
    assert "Not present in this crawl" in dossiers


async def test_rerunning_is_idempotent(tmp_path, library):
    """§12: existing rows update, no duplicates."""
    class TestPaths:
        db = tmp_path / "market.db"
        exports = tmp_path / "exports"

    with db.connect(TestPaths.db) as conn:
        db.upsert_domain(conn, {
            "domain": "tcoflyfishing.com", "root_url": "https://tcoflyfishing.com",
            "first_seen": "2026-08-01", "source_locators": "[]",
            "excluded": 0, "exclusion_reason": None,
        })

    for _ in range(2):
        clock = Clock()
        fetcher = PoliteFetcher(
            CrawlPolicy(), transport=make_handler(), clock=clock, sleep=clock.sleep
        )
        outcome = await crawl_one(fetcher, "tcoflyfishing.com", library)
        with db.connect(TestPaths.db) as conn:
            persist(conn, outcome, crawled_at="2026-08-01T00:00:00Z")
        await fetcher.aclose()

    conn = sqlite3.connect(TestPaths.db)
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("domains", "crawl_results", "products_raw",
                          "vendor_matches", "quality_signals", "scores")
        }
    finally:
        conn.close()

    assert counts["domains"] == 1
    assert counts["crawl_results"] == 1
    assert counts["quality_signals"] == 1
    assert counts["scores"] == 1
    assert counts["products_raw"] == 1200      # replaced, not appended
    assert counts["vendor_matches"] == 2       # Simms + Yeti
