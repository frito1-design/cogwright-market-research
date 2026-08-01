"""Stage 2 — platform detection (CW-RES-001 §4).

Detection is pure: it takes already-fetched artifacts and returns a verdict, so the
signal table can be tested without a network. Order matters — the checks run
most-specific first, because a Shopify store served behind a custom theme still emits
`cdn.shopify.com`, while a Squarespace asset host can appear on a site that merely
embeds one Squarespace widget.

`pos_platform` is always 'unknown'. §4 is explicit that Lightspeed Retail (R-Series) and
its peers are point-of-sale systems invisible to the public web. A Shopify-heavy result
is evidence about web platforms and about nothing else; it must not be read as evidence
that POS integration is unnecessary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

# (platform, [regexes]) — evaluated in order.
SIGNATURES: list[tuple[str, list[str]]] = [
    ("shopify", [r"cdn\.shopify\.com", r"Shopify\.shop", r"shopifycdn\.com", r"/cdn/shop/"]),
    ("bigcommerce", [r"cdn11\.bigcommerce\.com", r"bigcommerce\.com/s-", r"cdn\d*\.bigcommerce\.com"]),
    ("woocommerce", [r"wp-content/plugins/woocommerce", r"woocommerce-[a-z]", r"WooCommerce\s+\d"]),
    ("lightspeed_ecom", [r"cdn\.shoplightspeed\.com", r"shoplightspeed\.com"]),
    ("lightspeed_xseries", [r"vendhq\.com", r"secure\.vendhq\.com"]),
    ("squarespace", [r"static1\.squarespace\.com", r"squarespace-cdn\.com", r"squarespace\.com/config"]),
    ("wix", [r"wixstatic\.com", r"parastorage\.com", r"wix\.com/website"]),
    ("magento", [r"/static/version\d", r"Magento_[A-Z]", r"mage/cookies"]),
]

COMPILED = [(name, [re.compile(p, re.IGNORECASE) for p in pats]) for name, pats in SIGNATURES]

HEADER_SIGNALS = {
    "x-shopid": "shopify",
    "x-shopify-stage": "shopify",
    "x-shardid": "shopify",
    "x-bc-": "bigcommerce",
}

CART_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"add[\s_-]?to[\s_-]?cart",
        r'href="[^"]*/cart',
        r"\bshopping[\s_-]?cart\b",
        r"\bcart-?count\b",
        r"/checkout",
        r"data-cart",
    )
]


@dataclass
class PlatformVerdict:
    platform: str = "unknown"
    pos_platform: str = "unknown"  # §4: never inferred from the web surface
    has_cart: bool = False
    evidence: list[str] = field(default_factory=list)


def detect_platform(
    html: str,
    headers: dict[str, str] | None = None,
    *,
    robots_txt: str = "",
    products_json_ok: bool | None = None,
    cart_js_ok: bool | None = None,
) -> PlatformVerdict:
    """Classify a storefront from its homepage, headers, and optional probe results."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    evidence: list[str] = []

    for key, platform in HEADER_SIGNALS.items():
        for name in headers:
            if name.startswith(key):
                evidence.append(f"header:{name}")
                return PlatformVerdict(platform, has_cart=_has_cart(html, platform), evidence=evidence)

    # Shopify's endpoints are conclusive when they answer.
    if products_json_ok or cart_js_ok:
        evidence.append("endpoint:products.json" if products_json_ok else "endpoint:cart.js")
        return PlatformVerdict("shopify", has_cart=True, evidence=evidence)

    haystack = f"{html}\n{robots_txt}"
    for platform, patterns in COMPILED:
        for pattern in patterns:
            if pattern.search(haystack):
                evidence.append(f"html:{pattern.pattern}")
                return PlatformVerdict(platform, has_cart=_has_cart(html, platform), evidence=evidence)

    return PlatformVerdict("unknown", has_cart=_has_cart(html, "unknown"), evidence=evidence)


def _has_cart(html: str, platform: str) -> bool:
    if any(pattern.search(html) for pattern in CART_PATTERNS):
        return True
    # A hosted-cart platform with no visible cart markup is usually a JS-rendered cart.
    return platform in {"shopify", "bigcommerce", "lightspeed_ecom"}


def title_of(html: str) -> str:
    node = HTMLParser(html).css_first("title")
    return node.text(strip=True) if node else ""
