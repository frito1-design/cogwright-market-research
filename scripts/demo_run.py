#!/usr/bin/env python
"""Run the whole pipeline against synthetic shops and write the four deliverables.

No network. This exists so the shape of the output can be reviewed — and the scoring
argued with — before committing to an eight-hour live crawl.

    python scripts/demo_run.py [outdir]

Everything it writes is fake. The numbers are arithmetic on invented catalogs.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cogwright_research import db
from cogwright_research.config import CrawlPolicy
from cogwright_research.exporters import export_all
from cogwright_research.fetcher import PoliteFetcher
from cogwright_research.pipeline import run_crawl
from cogwright_research.vendors_match import VendorLibrary

REFERENCE = Path(__file__).resolve().parents[1] / "reference" / "vendors.csv"

SHOPIFY_HOME = (
    '<html><head><title>Shop</title>'
    '<script src="https://cdn.shopify.com/s/files/1/t.js"></script></head>'
    '<body><button>Add to Cart</button></body></html>'
)
WOO_HOME = (
    '<html><head><link href="/wp-content/plugins/woocommerce/a.css"></head>'
    '<body><a href="/cart">Cart</a></body></html>'
)
SQSP_HOME = '<html><head><img src="https://static1.squarespace.com/x.jpg"></head></html>'

# domain -> (size, vendor mix, share of products that are broken, platform, last update)
SHOPS = {
    "tcoflyfishing.com": (14_200, ["Simms", "Fishpond", "Hareline", "Yeti"], 0.55, "shopify", "2026-07-25"),
    "schultzoutfitters.com": (5_100, ["Simms", "Scientific Anglers", "Airflo"], 0.42, "shopify", "2026-07-20"),
    "charliesflyboxinc.com": (2_600, ["Hareline", "Whiting", "Semperfli", "SomeLocalTier"], 0.61, "shopify", "2026-07-28"),
    "driftlessangler.com": (1_800, [], 0.0, "woocommerce", "2026-06-30"),
    "dormantshop.com": (13_500, ["Simms", "Rio"], 0.5, "shopify", "2024-01-05"),
    "tinyshop.com": (240, ["Umpqua"], 0.1, "squarespace", "2026-07-01"),
}


def product(i: int, vendor: str, broken: bool, updated: str) -> dict:
    stamp = f"{updated}T00:00:00Z"
    if broken:
        return {
            "id": i, "title": "Fly", "vendor": vendor, "product_type": "", "tags": "",
            "published_at": stamp, "updated_at": stamp,
            "variants": [{"title": "Default Title", "price": "0.00", "sku": ""}],
            "images": [],
        }
    return {
        "id": i, "title": f"{vendor} Quality Product Number {i}", "vendor": vendor,
        "product_type": "Gear", "tags": ["gear", vendor.lower()],
        "published_at": stamp, "updated_at": stamp,
        "variants": [{"title": "One Size", "price": "49.95", "sku": f"SKU-{i}"}],
        "images": [{"src": "a.jpg"}],
    }


def build_transport() -> httpx.MockTransport:
    catalogs: dict[str, list[dict]] = {}
    for domain, (size, vendors, broken_share, platform, updated) in SHOPS.items():
        if platform != "shopify":
            continue
        catalogs[domain] = [
            product(i, vendors[i % len(vendors)], (i % 100) < broken_share * 100, updated)
            for i in range(1, size + 1)
        ]

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host.replace("www.", "")
        path = request.url.path
        page = int(request.url.params.get("page", 1) or 1)
        if host not in SHOPS:
            return httpx.Response(404)
        _, _, _, platform, updated = SHOPS[host]

        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /admin\nCrawl-delay: 1\n")
        if path == "/":
            body = {"shopify": SHOPIFY_HOME, "woocommerce": WOO_HOME}.get(platform, SQSP_HOME)
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        if path == "/products.json" and host in catalogs:
            return httpx.Response(200, json={"products": catalogs[host][(page - 1) * 250: page * 250]})
        if path == "/sitemap.xml" and platform != "shopify":
            size = SHOPS[host][0]
            locs = "".join(
                f"<url><loc>https://{host}/product/i-{i}</loc><lastmod>{updated}</lastmod></url>"
                for i in range(1, size + 1)
            )
            return httpx.Response(200, text=f"<urlset>{locs}</urlset>",
                                  headers={"content-type": "application/xml"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds  # demo runs instantly; the real crawl actually waits


async def main() -> int:
    out_root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("exports/demo")
    out_root.mkdir(parents=True, exist_ok=True)
    db_path = out_root / "demo.db"
    if db_path.exists():
        db_path.unlink()

    class DemoPaths:
        db = db_path
        exports = out_root

    locators = {"tcoflyfishing.com": 11, "schultzoutfitters.com": 6,
                "charliesflyboxinc.com": 4, "driftlessangler.com": 2,
                "dormantshop.com": 9, "tinyshop.com": 1}

    with db.connect(db_path) as conn:
        for domain, count in locators.items():
            db.upsert_domain(conn, {
                "domain": domain, "root_url": f"https://{domain}",
                "first_seen": "2026-08-01",
                "source_locators": json.dumps([f"locator:brand{i}" for i in range(count)]),
                "excluded": 0, "exclusion_reason": None,
            })

    clock = Clock()
    fetcher = PoliteFetcher(CrawlPolicy(), transport=build_transport(),
                            clock=clock, sleep=clock.sleep)
    library = VendorLibrary.from_csv(REFERENCE)
    await run_crawl(list(locators.items()), library, paths=DemoPaths, fetcher=fetcher)
    await fetcher.aclose()

    for name, path in export_all(db_path, out_root).items():
        print(f"{name:12} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
