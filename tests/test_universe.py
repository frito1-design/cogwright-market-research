from cogwright_research.universe import (
    LocatorSpec,
    Universe,
    classify_exclusion,
    extract_links_from_html,
    extract_links_from_json,
    normalize_domain,
)


def test_normalize_strips_scheme_www_path_and_case():
    for raw in (
        "https://WWW.TCOFlyFishing.com/collections/all?page=2",
        "tcoflyfishing.com",
        "http://www.tcoflyfishing.com",
    ):
        assert normalize_domain(raw) == "tcoflyfishing.com"


def test_normalize_keeps_registrable_domain_for_subdomains():
    assert normalize_domain("https://shop.example.com/x") == "example.com"
    # co.uk is a public suffix, so the registrable domain keeps one more label.
    assert normalize_domain("https://store.shop.example.co.uk") == "example.co.uk"
    assert normalize_domain("https://shop.example.com.au") == "example.com.au"


def test_normalize_rejects_non_websites():
    assert normalize_domain("") is None
    assert normalize_domain("localhost") is None
    assert normalize_domain("http://192.168.1.1/") is None


def test_exclusions_cover_chains_manufacturers_and_own_shop():
    assert classify_exclusion("cabelas.com") == "national_chain"
    assert classify_exclusion("simmsfishing.com") == "manufacturer"
    assert classify_exclusion("flyfishfood.com") == "own_company"
    assert classify_exclusion("someriverlodge.com") == "non_retail"
    assert classify_exclusion("tcoflyfishing.com") is None
    # "outfitters" must stay crawlable — plenty of real shops are named that way.
    assert classify_exclusion("schultzoutfitters.com") is None
    assert classify_exclusion("gruenoutfitters.com") is None


def test_universe_dedupes_and_unions_locator_sources():
    universe = Universe()
    universe.add("https://www.tcoflyfishing.com/", "locator:simms")
    universe.add("http://tcoflyfishing.com/pages/about", "locator:fishpond")
    universe.add("https://tcoflyfishing.com", "seed:recall")

    assert len(universe.candidates) == 1
    candidate = universe.candidates[0]
    assert candidate.domain == "tcoflyfishing.com"
    assert candidate.source_locators == {"locator:simms", "locator:fishpond", "seed:recall"}


def test_crawlable_omits_excluded():
    universe = Universe()
    universe.add("cabelas.com", "locator:simms")
    universe.add("tcoflyfishing.com", "locator:simms")
    assert [c.domain for c in universe.crawlable()] == ["tcoflyfishing.com"]


def test_extract_links_drops_own_host_social_and_relative():
    html = """
      <a href="https://www.tcoflyfishing.com/">TCO</a>
      <a href="https://facebook.com/simms">fb</a>
      <a href="/pages/local">relative</a>
      <a href="https://simmsfishing.com/about">self</a>
      <a href="https://duranglers.com/shop">Duranglers</a>
    """
    found = extract_links_from_html(html, "https://simmsfishing.com/pages/dealer-locator")
    assert found == ["tcoflyfishing.com", "duranglers.com"]


def test_extract_links_from_json_walks_the_configured_path():
    spec = LocatorSpec(name="x", url="https://x.com", kind="json",
                       array_path="data.dealers", website_key="url")
    payload = {"data": {"dealers": [
        {"name": "TCO", "url": "https://tcoflyfishing.com"},
        {"name": "No site"},
        {"name": "Social", "url": "https://instagram.com/shop"},
    ]}}
    assert extract_links_from_json(payload, spec) == ["tcoflyfishing.com"]


def test_extract_links_from_json_tolerates_a_wrong_path():
    spec = LocatorSpec(name="x", url="https://x.com", kind="json", array_path="nope.here")
    assert extract_links_from_json({"data": {}}, spec) == []


def test_seed_csv_skips_comment_header(tmp_path):
    path = tmp_path / "seed.csv"
    path.write_text(
        "# a comment\n# another\ndomain,source\ntcoflyfishing.com,seed:recall\n",
        encoding="utf-8",
    )
    universe = Universe()
    assert universe.load_seed_csv(path) == 1
    assert universe.candidates[0].domain == "tcoflyfishing.com"
