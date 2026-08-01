"""Stage 1 — build the domain universe (CW-RES-001 §3).

Two inputs feed the universe: brand dealer locators (structured, and the appearance list
is itself a scoring signal) and a seed CSV. Both funnel through `normalize_domain` so a
shop reached from six locators is one row with six `source_locators`.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from selectolax.parser import HTMLParser

from .fetcher import DomainAbandoned, PoliteFetcher

log = logging.getLogger(__name__)

# Second-level suffixes we care about; anything else is treated as a single-label TLD.
MULTIPART_SUFFIXES = frozenset(
    {"co.uk", "org.uk", "me.uk", "com.au", "net.au", "co.nz", "co.za", "com.mx", "co.jp"}
)

# Fly Fish Food is the operator's own shop, not a prospect. It will appear on most of the
# dealer locators we harvest, so it needs an explicit exclusion rather than a filter.
OWN_DOMAINS = frozenset({"flyfishfood.com", "cogwright.com"})

# National general-outdoor chains and marketplaces (§3 exclusions).
CHAIN_DOMAINS = frozenset(
    {
        "cabelas.com", "basspro.com", "sportsmans.com", "scheels.com", "rei.com",
        "dickssportinggoods.com", "amazon.com", "ebay.com", "walmart.com", "target.com",
        "backcountry.com", "sierra.com", "academy.com", "fieldandstream.com",
    }
)

# The brands whose locators we harvest are manufacturers, not prospects.
MANUFACTURER_DOMAINS = frozenset(
    {
        "simmsfishing.com", "sageflyfish.com", "scottflyrod.com", "winstonrods.com",
        "thomasandthomas.com", "douglasoutdoors.com", "tforods.com", "echoflyfishing.com",
        "redington.com", "rossreels.com", "abelreels.com", "hatchoutdoors.com",
        "nautilusreels.com", "rioproducts.com", "scientificanglers.com", "airflousa.com",
        "fishpondusa.com", "umpqua.com", "loonoutdoors.com", "costadelmar.com",
        "smithoptics.com", "yeti.com", "patagonia.com", "howlerbros.com", "korkers.com",
        "orvis.com", "hareline.com", "whitingfarms.com", "semperfli.net", "fullingmill.com",
        "waterworks-lamson.com", "galvanflyreels.com", "cortlandline.com", "hardyfishing.com",
        "grundens.com", "skwala.com", "duckcamp.com", "rep-your-water.com", "tacky.com",
        "montanaflycompany.com", "wapsifly.com", "danielsonintl.com", "renzetti.com",
        "regalvise.com", "drslick.com", "cheekyfishing.com", "trxstle.com", "riversmith.com",
    }
)

# Substrings that mark a guide service, lodge, or club rather than a retailer. Domain
# labels have no word separators, so these match anywhere in the name — which does mean
# the occasional false positive ("lodgepole..."). That is deliberate and cheap to undo:
# excluded domains are kept in the `domains` table with their reason rather than dropped,
# so a review pass can reinstate one by clearing the flag.
#
# "outfitters" is *not* in this list. It is ordinary in legitimate fly shop names
# (Schultz Outfitters is a design partner), and the real filter for a guide service is
# Stage 2 finding no cart and Stage 6 assigning tier D.
NON_RETAIL_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"lodge", r"guideservice", r"guidedtrips", r"charters?\.", r"fishingtrips",
        r"dayfloat", r"troutunlimited", r"\bchapter", r"association", r"flyfishingschool",
    )
)


@dataclass
class Candidate:
    domain: str
    root_url: str
    source_locators: set[str] = field(default_factory=set)
    excluded: bool = False
    exclusion_reason: str | None = None


def normalize_domain(url: str) -> str | None:
    """Reduce any URL or bare hostname to a lowercase registrable domain.

    Returns None for values that are not plausibly a website (empty, IP literal,
    localhost, or a hostname with no dot).
    """
    if not url:
        return None
    raw = url.strip().lower()
    if not raw:
        return None
    if "//" not in raw:
        raw = f"http://{raw}"
    host = urlparse(raw).hostname or ""
    host = host.strip(".")
    if not host or "." not in host:
        return None
    if re.fullmatch(r"[\d.]+", host):  # bare IPv4
        return None
    host = host.removeprefix("www.")

    labels = host.split(".")
    if len(labels) <= 2:
        return host
    tail2 = ".".join(labels[-2:])
    keep = 3 if tail2 in MULTIPART_SUFFIXES else 2
    return ".".join(labels[-keep:])


def classify_exclusion(domain: str) -> str | None:
    """Return an exclusion reason, or None if the domain should be crawled."""
    if domain in OWN_DOMAINS:
        return "own_company"
    if domain in CHAIN_DOMAINS:
        return "national_chain"
    if domain in MANUFACTURER_DOMAINS:
        return "manufacturer"
    for pattern in NON_RETAIL_PATTERNS:
        if pattern.search(domain):
            return "non_retail"
    return None


class Universe:
    """Accumulates candidates from every source, deduped on registrable domain."""

    def __init__(self) -> None:
        self._candidates: dict[str, Candidate] = {}

    def add(self, url: str, source: str) -> Candidate | None:
        domain = normalize_domain(url)
        if domain is None:
            return None
        existing = self._candidates.get(domain)
        if existing is None:
            reason = classify_exclusion(domain)
            existing = Candidate(
                domain=domain,
                root_url=f"https://{domain}",
                excluded=reason is not None,
                exclusion_reason=reason,
            )
            self._candidates[domain] = existing
        existing.source_locators.add(source)
        return existing

    def add_many(self, urls: Iterable[str], source: str) -> int:
        before = len(self._candidates)
        for url in urls:
            self.add(url, source)
        return len(self._candidates) - before

    @property
    def candidates(self) -> list[Candidate]:
        return sorted(self._candidates.values(), key=lambda c: c.domain)

    def crawlable(self) -> list[Candidate]:
        return [c for c in self.candidates if not c.excluded]

    def load_seed_csv(self, path: Path) -> int:
        """Load `seeds/domains_seed.csv` (columns: domain, source).

        Leading `#` lines are stripped before parsing so the file can carry the caveats
        that matter about where its contents came from.
        """
        if not Path(path).exists():
            return 0
        lines = [
            line
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        added = 0
        for row in csv.DictReader(lines):
            domain = (row.get("domain") or "").strip()
            if not domain:
                continue
            if self.add(domain, (row.get("source") or "seed").strip()):
                added += 1
        return added

    def to_rows(self, first_seen: str) -> list[dict[str, Any]]:
        return [
            {
                "domain": c.domain,
                "root_url": c.root_url,
                "first_seen": first_seen,
                "source_locators": json.dumps(sorted(c.source_locators)),
                "excluded": int(c.excluded),
                "exclusion_reason": c.exclusion_reason,
            }
            for c in self.candidates
        ]


# --------------------------------------------------------------------------------------
# Dealer locator harvesting
# --------------------------------------------------------------------------------------


@dataclass
class LocatorSpec:
    """One brand's dealer locator, as declared in reference/dealer_locators.yaml."""

    name: str
    url: str
    kind: str = "html"  # html | json
    #: For kind=json, dotted path to the dealer array (e.g. "data.dealers").
    array_path: str = ""
    #: For kind=json, the key holding the dealer's website.
    website_key: str = "website"
    #: True when the URL has been confirmed to exist and to be this brand's locator.
    url_confirmed: bool = False
    #: Stricter than url_confirmed: a human has opened the page and confirmed it lists
    #: retailers *with website links*, and that `kind`/`array_path` match the payload.
    verified: bool = False
    #: Set False to keep an entry on record without harvesting it — e.g. a brand with no
    #: public dealer list, where a guessed URL would return noise rather than dealers.
    enabled: bool = True
    notes: str = ""


def load_locator_specs(path: Path, *, include_disabled: bool = False) -> list[LocatorSpec]:
    """Load the locator table. Disabled entries are skipped unless asked for."""
    if not Path(path).exists():
        return []
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    specs = [LocatorSpec(**entry) for entry in raw.get("locators", [])]
    return specs if include_disabled else [s for s in specs if s.enabled]


def extract_links_from_html(html: str, locator_url: str) -> list[str]:
    """Pull outbound absolute links, dropping the brand's own host and common noise."""
    own = normalize_domain(locator_url)
    out: list[str] = []
    for node in HTMLParser(html).css("a[href]"):
        href = node.attributes.get("href") or ""
        if not href.startswith(("http://", "https://")):
            continue
        domain = normalize_domain(href)
        if domain is None or domain == own:
            continue
        if _is_social_or_infra(domain):
            continue
        out.append(domain)
    return out


def extract_links_from_json(payload: Any, spec: LocatorSpec) -> list[str]:
    node: Any = payload
    for part in filter(None, spec.array_path.split(".")):
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return []
    if not isinstance(node, list):
        return []
    out: list[str] = []
    for entry in node:
        if not isinstance(entry, dict):
            continue
        website = entry.get(spec.website_key)
        domain = normalize_domain(website) if isinstance(website, str) else None
        if domain and not _is_social_or_infra(domain):
            out.append(domain)
    return out


SOCIAL_OR_INFRA = frozenset(
    {
        "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
        "tiktok.com", "linkedin.com", "pinterest.com", "google.com", "maps.google.com",
        "goo.gl", "apple.com", "shopify.com", "wordpress.com", "wix.com", "squarespace.com",
        "yelp.com", "tripadvisor.com", "vimeo.com", "mailchimp.com", "gmail.com",
    }
)


def _is_social_or_infra(domain: str) -> bool:
    return domain in SOCIAL_OR_INFRA


async def harvest_locator(
    fetcher: PoliteFetcher, spec: LocatorSpec, universe: Universe
) -> int:
    """Fetch one dealer locator and fold its domains into the universe."""
    domain = normalize_domain(spec.url)
    if domain is None:
        log.warning("locator %s has an unusable URL: %s", spec.name, spec.url)
        return 0
    try:
        async with fetcher.crawl_domain(domain):
            result = await fetcher.get(domain, spec.url)
    except DomainAbandoned:
        log.warning("locator %s abandoned (rate limited)", spec.name)
        return 0
    if result is None or result.status >= 400:
        log.warning("locator %s unavailable (status=%s)", spec.name, getattr(result, "status", None))
        return 0

    if spec.kind == "json":
        try:
            domains = extract_links_from_json(result.json(), spec)
        except ValueError:
            log.warning("locator %s returned non-JSON", spec.name)
            return 0
    else:
        domains = extract_links_from_html(result.text, spec.url)

    return universe.add_many(domains, f"locator:{spec.name}")
