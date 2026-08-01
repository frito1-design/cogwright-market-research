"""Stage orchestration: one domain in, one scored row out."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import db
from .catalog import CatalogResult, crawl_shopify_catalog, crawl_sitemap_catalog
from .config import Paths
from .fetcher import DomainAbandoned, PoliteFetcher
from .platform_detect import PlatformVerdict, detect_platform
from .quality import QualitySignals, compute_quality
from .scoring import Score, score_domain
from .vendors_match import OverlapReport, VendorLibrary

log = logging.getLogger(__name__)


@dataclass
class DomainOutcome:
    domain: str
    http_status: int | None = None
    final_url: str | None = None
    tls_valid: bool = True
    platform: str = "unknown"
    has_cart: bool = False
    robots_blocked: bool = False
    catalog: CatalogResult = field(default_factory=CatalogResult)
    overlap: OverlapReport = field(default_factory=OverlapReport)
    quality: QualitySignals = field(default_factory=QualitySignals)
    score: Score | None = None
    error: str | None = None


async def crawl_one(
    fetcher: PoliteFetcher,
    domain: str,
    library: VendorLibrary,
    *,
    locator_count: int = 0,
    now: datetime | None = None,
) -> DomainOutcome:
    """Run stages 2-6 for a single domain."""
    outcome = DomainOutcome(domain=domain)
    try:
        async with fetcher.crawl_domain(domain):
            home = await fetcher.get(domain, "/")
            if home is None:
                robots = fetcher.robots_for(domain)
                outcome.robots_blocked = robots is not None and not robots.can_fetch(
                    fetcher.policy.user_agent, "/"
                )
                outcome.error = "homepage_unavailable"
                return outcome

            outcome.http_status = home.status
            outcome.final_url = home.final_url
            outcome.tls_valid = home.final_url.startswith("https://")

            robots_doc = ""
            robots_result = await fetcher.get(domain, "/robots.txt", allow_status=(404,))
            if robots_result is not None:
                robots_doc = robots_result.text

            verdict: PlatformVerdict = detect_platform(
                home.text, home.headers, robots_txt=robots_doc
            )
            outcome.platform = verdict.platform
            outcome.has_cart = verdict.has_cart

            catalog: CatalogResult | None = None
            if verdict.platform == "shopify":
                catalog = await crawl_shopify_catalog(fetcher, domain, now=now)
                if catalog is None:
                    # ~15-20% of Shopify shops disable /products.json (§5).
                    log.info("%s: products.json unavailable, falling back to sitemap", domain)
            if catalog is None:
                catalog = await crawl_sitemap_catalog(fetcher, domain, now=now)
            outcome.catalog = catalog

            if catalog.products:
                counts: Counter[str] = Counter(
                    p.vendor for p in catalog.products if p.vendor.strip()
                )
                outcome.overlap = library.analyze(counts)
                outcome.quality = compute_quality(catalog.products)

    except DomainAbandoned:
        outcome.error = "abandoned_rate_limited"
        return outcome

    outcome.score = score_domain(
        catalog_size=outcome.catalog.catalog_size,
        platform=outcome.platform,
        vendor_overlap_weighted=outcome.overlap.vendor_overlap_weighted,
        defect_density=outcome.quality.defect_density,
        dormant=outcome.catalog.dormant,
        has_cart=outcome.has_cart,
        locator_count=locator_count,
    )
    return outcome


def persist(conn, outcome: DomainOutcome, *, crawled_at: str) -> None:
    """Write one outcome. Idempotent: a re-run updates the same rows (§12)."""
    db.upsert_crawl_result(
        conn,
        {
            "domain": outcome.domain,
            "crawled_at": crawled_at,
            "http_status": outcome.http_status,
            "platform": outcome.platform,
            "pos_platform": "unknown",  # §4: never inferred from the web surface
            "has_cart": int(outcome.has_cart),
            "robots_blocked": int(outcome.robots_blocked),
            "catalog_size": outcome.catalog.catalog_size,
            "variant_count": outcome.catalog.variant_count,
            "catalog_method": outcome.catalog.catalog_method,
            "truncated": int(outcome.catalog.truncated),
            "days_since_last_product_update": outcome.catalog.days_since_last_product_update,
            "dormant": int(outcome.catalog.dormant),
            "final_url": outcome.final_url,
            "tls_valid": int(outcome.tls_valid),
            "last_modified": outcome.catalog.last_modified,
        },
    )

    db.replace_products(
        conn,
        outcome.domain,
        [
            {
                "domain": outcome.domain,
                "product_id": p.product_id,
                "title": p.title,
                "vendor": p.vendor,
                "product_type": p.product_type,
                "tags": p.tags,
                "variant_count": p.variant_count,
                "image_count": p.image_count,
                "min_price": p.min_price,
                "published_at": p.published_at,
                "updated_at": p.updated_at,
                "no_sku_variants": p.no_sku_variants,
                "default_title_only": p.default_title_only,
                "zero_price_variants": p.zero_price_variants,
            }
            for p in outcome.catalog.products
        ],
    )

    db.replace_vendor_matches(
        conn,
        outcome.domain,
        [
            {
                "domain": outcome.domain,
                "vendor_raw": m.vendor_raw,
                "vendor_canonical": m.vendor_canonical,
                "match_type": m.match_type,
                "confidence": m.confidence,
                "product_count": m.product_count,
            }
            for m in outcome.overlap.matches
        ],
    )

    if outcome.catalog.products:
        db.upsert_quality(conn, {"domain": outcome.domain, **outcome.quality.as_row()})

    if outcome.score is not None:
        db.upsert_score(
            conn,
            {
                "domain": outcome.domain,
                "tier": outcome.score.tier,
                "fit_score": outcome.score.fit_score,
                "size_pts": outcome.score.size_pts,
                "platform_pts": outcome.score.platform_pts,
                "vendor_pts": outcome.score.vendor_pts,
                "defect_pts": outcome.score.defect_pts,
                "modifiers": json.dumps(outcome.score.modifiers),
                "scored_at": crawled_at,
            },
        )


async def run_crawl(
    domains: Sequence[tuple[str, int]],
    library: VendorLibrary,
    *,
    paths: Paths | None = None,
    fetcher: PoliteFetcher | None = None,
    progress_every: int = 25,
) -> list[DomainOutcome]:
    """Crawl every (domain, locator_count) pair, persisting as results arrive."""
    paths = paths or Paths()
    owns_fetcher = fetcher is None
    fetcher = fetcher or PoliteFetcher()
    crawled_at = datetime.now(UTC).isoformat()
    outcomes: list[DomainOutcome] = []

    try:
        tasks = [
            asyncio.create_task(crawl_one(fetcher, domain, library, locator_count=count))
            for domain, count in domains
        ]
        with db.connect(paths.db) as conn:
            for index, task in enumerate(asyncio.as_completed(tasks), start=1):
                outcome = await task
                outcomes.append(outcome)
                persist(conn, outcome, crawled_at=crawled_at)
                if index % progress_every == 0:
                    conn.commit()
                    log.info("crawled %d/%d domains", index, len(tasks))
            db.log_requests(conn, fetcher.request_log)
            for domain in sorted(fetcher.pii_domains):
                log.warning("PII observed, response discarded, manual review: %s", domain)
    finally:
        if owns_fetcher:
            await fetcher.aclose()

    violations = fetcher.violations()
    if violations:  # must never happen; §12 acceptance criterion
        log.error("robots.txt violations detected: %d", len(violations))
    return outcomes
