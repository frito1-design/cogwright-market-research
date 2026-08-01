"""Stage 3 — catalog inventory (CW-RES-001 §5).

Two paths. The Shopify `/products.json` path is preferred because one endpoint yields
SKU count, variant count, vendor mix, category coverage, pricing, image coverage and
liveness together. Everything else falls back to counting product URLs in the sitemap —
individual product pages are never fetched (§13), which keeps the crawl cheap and polite
at the cost of the quality signals in §7.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import DORMANT_AFTER_DAYS, SHOPIFY_MAX_PAGES, SHOPIFY_PAGE_LIMIT
from .fetcher import PoliteFetcher

log = logging.getLogger(__name__)

PRODUCT_URL_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (r"/products/", r"/product/", r"/shop/[^/]+/[^/]+", r"/p/", r"/item/", r"/pd/")
]
# Sitemap URLs that look like products but are collections, pages, or blog posts.
NON_PRODUCT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (r"/collections/[^/]+/?$", r"/blogs?/", r"/pages/", r"/policies/", r"\.(jpg|png|webp)$")
]

MAX_SITEMAP_DOCS = 60  # bound on nested sitemap fetches per domain
LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
LASTMOD_RE = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.IGNORECASE | re.DOTALL)
SITEMAPINDEX_RE = re.compile(r"<sitemapindex", re.IGNORECASE)


@dataclass
class ProductRecord:
    product_id: str
    title: str
    vendor: str
    product_type: str
    tags: str
    variant_count: int
    image_count: int
    min_price: float | None
    published_at: str | None
    updated_at: str | None
    no_sku_variants: int = 0
    default_title_only: int = 0
    zero_price_variants: int = 0


@dataclass
class CatalogResult:
    catalog_size: int | None = None
    variant_count: int | None = None
    catalog_method: str = "undetermined"  # shopify_products_json | sitemap | undetermined
    truncated: bool = False
    days_since_last_product_update: int | None = None
    dormant: bool = False
    products: list[ProductRecord] = field(default_factory=list)
    last_modified: str | None = None


def parse_products(payload: Any) -> list[ProductRecord]:
    """Normalize one page of Shopify `/products.json` into records."""
    items = payload.get("products", []) if isinstance(payload, dict) else []
    out: list[ProductRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        variants = [v for v in (item.get("variants") or []) if isinstance(v, dict)]
        images = item.get("images") or []
        prices = [_to_float(v.get("price")) for v in variants]
        prices = [p for p in prices if p is not None]
        no_sku = sum(1 for v in variants if not str(v.get("sku") or "").strip())
        zero_priced = sum(1 for p in prices if p == 0.0)
        default_only = int(
            len(variants) == 1
            and str(variants[0].get("title") or "").strip().lower() == "default title"
        )
        out.append(
            ProductRecord(
                product_id=str(item.get("id") or ""),
                title=str(item.get("title") or ""),
                vendor=str(item.get("vendor") or "").strip(),
                product_type=str(item.get("product_type") or "").strip(),
                tags=_tags_to_str(item.get("tags")),
                variant_count=len(variants),
                image_count=len(images) if isinstance(images, list) else 0,
                min_price=min(prices) if prices else None,
                published_at=item.get("published_at"),
                updated_at=item.get("updated_at"),
                no_sku_variants=no_sku,
                default_title_only=default_only,
                zero_price_variants=zero_priced,
            )
        )
    return out


async def crawl_shopify_catalog(
    fetcher: PoliteFetcher, domain: str, *, now: datetime | None = None
) -> CatalogResult | None:
    """Paginate `/products.json` until an empty page. Returns None if unavailable.

    Roughly 15-20% of Shopify shops disable this endpoint; the caller falls back to the
    sitemap path for those.
    """
    products: list[ProductRecord] = []
    seen_ids: set[str] = set()
    truncated = False

    for page in range(1, SHOPIFY_MAX_PAGES + 1):
        path = f"/products.json?limit={SHOPIFY_PAGE_LIMIT}&page={page}"
        # DomainAbandoned propagates to the caller: once a shop has rate-limited us
        # three times, we stop touching it entirely rather than move to the next path.
        result = await fetcher.get(domain, path)
        if result is None or result.status != 200:
            if page == 1:
                return None
            break
        try:
            batch = parse_products(result.json())
        except ValueError:
            if page == 1:
                return None
            break
        if not batch:
            break
        # Some shops ignore `page` and re-serve page 1 forever; stop when nothing is new.
        fresh = [p for p in batch if p.product_id and p.product_id not in seen_ids]
        if not fresh:
            break
        seen_ids.update(p.product_id for p in fresh)
        products.extend(fresh)
        if page == SHOPIFY_MAX_PAGES:
            truncated = True

    if not products:
        return None

    age = _days_since(max((p.updated_at for p in products if p.updated_at), default=None), now)
    return CatalogResult(
        catalog_size=len(products),
        variant_count=sum(p.variant_count for p in products),
        catalog_method="shopify_products_json",
        truncated=truncated,
        days_since_last_product_update=age,
        dormant=age is not None and age > DORMANT_AFTER_DAYS,
        products=products,
        last_modified=max((p.updated_at for p in products if p.updated_at), default=None),
    )


async def crawl_sitemap_catalog(
    fetcher: PoliteFetcher, domain: str, *, now: datetime | None = None
) -> CatalogResult:
    """Count product URLs across the sitemap tree. Never fetches a product page (§13)."""
    queue: list[str] = ["/sitemap.xml"]
    visited: set[str] = set()
    product_urls: set[str] = set()
    lastmods: list[str] = []
    docs = 0

    while queue and docs < MAX_SITEMAP_DOCS:
        target = queue.pop(0)
        if target in visited:
            continue
        visited.add(target)
        result = await fetcher.get(domain, target)  # DomainAbandoned propagates
        docs += 1
        if result is None or result.status != 200 or "<loc" not in result.text.lower():
            continue

        locs = LOC_RE.findall(result.text)
        lastmods.extend(LASTMOD_RE.findall(result.text))
        if SITEMAPINDEX_RE.search(result.text):
            queue.extend(loc for loc in locs if loc not in visited)
            continue
        product_urls.update(loc for loc in locs if is_product_url(loc))

    if not product_urls:
        return CatalogResult(catalog_size=None, catalog_method="undetermined")

    newest = max(lastmods, default=None)
    age = _days_since(newest, now)
    return CatalogResult(
        catalog_size=len(product_urls),
        variant_count=None,
        catalog_method="sitemap",
        truncated=docs >= MAX_SITEMAP_DOCS,
        days_since_last_product_update=age,
        dormant=age is not None and age > DORMANT_AFTER_DAYS,
        last_modified=newest,
    )


def is_product_url(url: str) -> bool:
    if any(pattern.search(url) for pattern in NON_PRODUCT_PATTERNS):
        return False
    return any(pattern.search(url) for pattern in PRODUCT_URL_PATTERNS)


def _tags_to_str(tags: Any) -> str:
    if isinstance(tags, list):
        return ", ".join(str(t).strip() for t in tags if str(t).strip())
    return str(tags or "").strip()


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _days_since(stamp: str | None, now: datetime | None = None) -> int | None:
    parsed = parse_timestamp(stamp)
    if parsed is None:
        return None
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return max((reference - parsed).days, 0)


def parse_timestamp(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    text = str(stamp).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(text[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def newest_timestamp(stamps: Iterable[str | None]) -> str | None:
    parsed = [(parse_timestamp(s), s) for s in stamps]
    valid = [(p, s) for p, s in parsed if p is not None]
    return max(valid, key=lambda pair: pair[0])[1] if valid else None
