"""Crawl configuration.

The politeness settings in `CrawlPolicy` are hard floors, not tunables. CW-RES-001 §9
makes them non-negotiable, so `from_env` clamps rather than trusts: an operator can make
the crawl slower and quieter, never faster or louder.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

USER_AGENT = (
    "Cogwright-Research/1.0 (+https://cogwright.com/research; contact@cogwright.com)"
)

# Floors from §9. Environment overrides may only move these in the polite direction.
MIN_DELAY_PER_DOMAIN = 2.0
MAX_CONCURRENT_DOMAINS = 8

DORMANT_AFTER_DAYS = 180
SHOPIFY_PAGE_LIMIT = 250
SHOPIFY_MAX_PAGES = 60  # 15,000 products, then truncated=True
FUZZY_MIN_CONFIDENCE = 88

# Generic one-word vendor names ("Peak", "Surface", "Alpine") collide with ordinary
# English in Shopify vendor strings. Fuzzy matching is withheld from short single-token
# canonicals; they must match exactly or via a registered alias. See vendors_match.py.
FUZZY_MIN_TOKENS = 2
FUZZY_MIN_CHARS = 8

# Bookkeeping rows in the Hook vendor table that are not merchandise brands. Kept in
# reference/vendors.csv for fidelity with the source, ignored for overlap scoring.
NON_BRAND_CANONICALS = frozenset(
    {
        "amazon",
        "vendor-unknown",
        "flies - vendors",
        "fly fish food",
        "fly fish food custom",
        "umpqua consignment",
        "whitingold",
        "rise.ai",
        "route",
    }
)


@dataclass(frozen=True)
class CrawlPolicy:
    """Rate limiting and identification. Immutable once built."""

    user_agent: str = USER_AGENT
    min_delay_per_domain: float = MIN_DELAY_PER_DOMAIN
    max_concurrent_domains: int = MAX_CONCURRENT_DOMAINS
    request_timeout: float = 30.0
    max_retries: int = 3
    obey_robots: bool = True

    @classmethod
    def from_env(cls) -> CrawlPolicy:
        delay = _env_float("CW_MIN_DELAY_PER_DOMAIN", MIN_DELAY_PER_DOMAIN)
        conc = _env_int("CW_MAX_CONCURRENT_DOMAINS", MAX_CONCURRENT_DOMAINS)
        return cls(
            min_delay_per_domain=max(delay, MIN_DELAY_PER_DOMAIN),
            max_concurrent_domains=min(max(conc, 1), MAX_CONCURRENT_DOMAINS),
            request_timeout=_env_float("CW_REQUEST_TIMEOUT", 30.0),
        )


@dataclass(frozen=True)
class Paths:
    root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2])

    @property
    def db(self) -> Path:
        return self.root / os.getenv("CW_DB_PATH", "data/market.db")

    @property
    def reference(self) -> Path:
        return self.root / "reference"

    @property
    def seeds(self) -> Path:
        return self.root / "seeds"

    @property
    def exports(self) -> Path:
        return self.root / "exports"


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "") or default)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default
