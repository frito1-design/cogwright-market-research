from datetime import UTC, datetime

from cogwright_research.catalog import (
    CatalogResult,
    is_product_url,
    parse_products,
    parse_timestamp,
)
from cogwright_research.platform_detect import detect_platform

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def test_detects_each_platform_from_its_signature():
    cases = {
        "shopify": '<script src="https://cdn.shopify.com/s/files/theme.js"></script>',
        "bigcommerce": '<img src="https://cdn11.bigcommerce.com/s-abc/logo.png">',
        "woocommerce": '<link href="/wp-content/plugins/woocommerce/assets/css/woo.css">',
        "lightspeed_ecom": '<script src="https://cdn.shoplightspeed.com/shops/1/app.js">',
        "lightspeed_xseries": '<a href="https://secure.vendhq.com/checkout">Checkout</a>',
        "squarespace": '<img src="https://static1.squarespace.com/x.jpg">',
        "wix": '<script src="https://static.parastorage.com/services/x.js">',
        "magento": '<script src="/static/version1234/frontend/Magento_Ui/js/x.js">',
    }
    for expected, html in cases.items():
        assert detect_platform(html).platform == expected, expected


def test_unknown_when_nothing_matches():
    assert detect_platform("<html><body>Hi</body></html>").platform == "unknown"


def test_headers_and_endpoints_are_conclusive():
    assert detect_platform("<html></html>", {"X-ShopId": "123"}).platform == "shopify"
    assert detect_platform("<html></html>", products_json_ok=True).platform == "shopify"


def test_pos_platform_is_always_unknown():
    """§4: POS is invisible from the public web and must never be inferred."""
    verdict = detect_platform('<script src="https://cdn.shopify.com/x.js">')
    assert verdict.platform == "shopify"
    assert verdict.pos_platform == "unknown"


def test_has_cart_from_markup_and_from_hosted_platform():
    assert detect_platform('<button>Add to Cart</button>').has_cart
    assert detect_platform('<script src="https://cdn.shopify.com/x.js">').has_cart
    assert not detect_platform("<html><p>Guide service</p></html>").has_cart


def test_parse_products_derives_defect_counters():
    payload = {
        "products": [
            {
                "id": 1, "title": "Simms G3 Guide Waders", "vendor": "Simms",
                "product_type": "Waders", "tags": ["waders", "simms"],
                "published_at": "2025-01-01T00:00:00-07:00",
                "updated_at": "2026-07-01T00:00:00-07:00",
                "variants": [
                    {"title": "M", "price": "749.95", "sku": "SG3-M"},
                    {"title": "L", "price": "749.95", "sku": ""},
                ],
                "images": [{"src": "a.jpg"}],
            },
            {
                "id": 2, "title": "Thing", "vendor": "", "product_type": "", "tags": "",
                "updated_at": "2026-07-15T00:00:00Z",
                "variants": [{"title": "Default Title", "price": "0.00", "sku": ""}],
                "images": [],
            },
        ]
    }
    products = parse_products(payload)
    assert [p.product_id for p in products] == ["1", "2"]

    first, second = products
    assert first.variant_count == 2 and first.no_sku_variants == 1
    assert first.image_count == 1 and first.min_price == 749.95
    assert first.tags == "waders, simms" and first.default_title_only == 0

    assert second.default_title_only == 1
    assert second.zero_price_variants == 1
    assert second.no_sku_variants == 1
    assert second.min_price == 0.0


def test_parse_products_tolerates_junk():
    assert parse_products({}) == []
    assert parse_products({"products": [None, "x"]}) == []
    assert parse_products([]) == []


def test_product_url_matching_excludes_collections_and_blogs():
    assert is_product_url("https://x.com/products/simms-waders")
    assert is_product_url("https://x.com/product/123")
    assert not is_product_url("https://x.com/collections/waders")
    assert not is_product_url("https://x.com/blogs/news/spring-runoff")
    assert not is_product_url("https://x.com/pages/about")
    assert not is_product_url("https://x.com/products/img.jpg")


def test_parse_timestamp_handles_shopify_and_sitemap_formats():
    assert parse_timestamp("2026-07-01T00:00:00-07:00").year == 2026
    assert parse_timestamp("2026-07-01T00:00:00Z").tzinfo is not None
    assert parse_timestamp("2026-07-01").month == 7
    assert parse_timestamp("garbage") is None
    assert parse_timestamp(None) is None


def test_catalog_result_defaults_to_undetermined():
    """§5: an unknown catalog size is null, never a guess."""
    result = CatalogResult()
    assert result.catalog_size is None
    assert result.catalog_method == "undetermined"
    assert result.dormant is False
