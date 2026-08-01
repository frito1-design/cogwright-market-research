"""The §9 constraints are the ones that must not regress, so they are tested directly."""

import httpx
import pytest

from cogwright_research.config import CrawlPolicy
from cogwright_research.fetcher import DomainAbandoned, PoliteFetcher


class FakeClock:
    """Deterministic monotonic clock; `sleep` advances it instead of waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def build(handler, clock: FakeClock, **policy_kwargs) -> PoliteFetcher:
    policy = CrawlPolicy(request_timeout=5.0, **policy_kwargs)
    return PoliteFetcher(
        policy, transport=httpx.MockTransport(handler), clock=clock, sleep=clock.sleep
    )


async def test_identifies_itself_honestly():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, text="ok")

    clock = FakeClock()
    async with build(handler, clock) as fetcher:
        await fetcher.get("example.com", "/")
    assert seen["ua"] == (
        "Cogwright-Research/1.0 (+https://cogwright.com/research; contact@cogwright.com)"
    )


async def test_never_requests_a_disallowed_path():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /products.json\n")
        return httpx.Response(200, text="body")

    clock = FakeClock()
    async with build(handler, clock) as fetcher:
        assert await fetcher.get("example.com", "/products.json?limit=250") is None
        assert await fetcher.get("example.com", "/") is not None

    assert "/products.json" not in requested
    outcomes = [r.outcome for r in fetcher.request_log]
    assert "robots_blocked" in outcomes
    assert fetcher.violations() == []


async def test_waits_at_least_two_seconds_between_requests_to_one_domain():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    clock = FakeClock()
    async with build(handler, clock) as fetcher:
        await fetcher.get("example.com", "/a")
        await fetcher.get("example.com", "/b")
        await fetcher.get("example.com", "/c")

    # robots.txt plus three pages: every gap after the first is >= the 2s floor.
    assert all(s >= 2.0 for s in clock.slept)
    assert len(clock.slept) >= 3


async def test_declared_crawl_delay_wins_when_it_is_slower():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 9\n")
        return httpx.Response(200, text="ok")

    clock = FakeClock()
    async with build(handler, clock) as fetcher:
        await fetcher.get("example.com", "/a")
        await fetcher.get("example.com", "/b")
    assert max(clock.slept) >= 9.0


async def test_a_faster_delay_in_robots_cannot_beat_our_floor():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 0.1\n")
        return httpx.Response(200, text="ok")

    clock = FakeClock()
    async with build(handler, clock) as fetcher:
        await fetcher.get("example.com", "/a")
        await fetcher.get("example.com", "/b")
    assert all(s >= 2.0 for s in clock.slept)


async def test_policy_floors_cannot_be_loosened_by_configuration():
    loose = CrawlPolicy(min_delay_per_domain=0.01, max_concurrent_domains=500)
    # The dataclass accepts anything; from_env is where operator input is clamped.
    assert loose.min_delay_per_domain == 0.01
    clamped = CrawlPolicy.from_env()
    assert clamped.min_delay_per_domain >= 2.0
    assert clamped.max_concurrent_domains <= 8


async def test_repeated_429_abandons_the_domain():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(429, headers={"retry-after": "1"})

    clock = FakeClock()
    async with build(handler, clock) as fetcher:
        with pytest.raises(DomainAbandoned):
            await fetcher.get("example.com", "/")
        with pytest.raises(DomainAbandoned):
            await fetcher.get("example.com", "/other")  # stays abandoned

    assert any(r.outcome == "abandoned" for r in fetcher.request_log)


async def test_transient_503_is_retried_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else httpx.Response(200, text="ok")

    clock = FakeClock()
    async with build(handler, clock) as fetcher:
        result = await fetcher.get("example.com", "/")
    assert result is not None and result.status == 200


async def test_customer_data_is_discarded_and_the_domain_is_flagged():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={"orders": [{"id": 1, "email": "a@b.com", "billing_address": {"zip": "1"}}]},
        )

    clock = FakeClock()
    async with build(handler, clock) as fetcher:
        assert await fetcher.get("example.com", "/orders.json") is None

    assert "example.com" in fetcher.pii_domains
    assert any(r.outcome == "pii_discarded" for r in fetcher.request_log)


async def test_ordinary_product_json_is_not_mistaken_for_pii():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={"products": [
                {"id": 1, "title": "Waders", "vendor": "Simms",
                 "variants": [{"sku": "A", "price": "1.00"}], "images": []}
            ]},
        )

    clock = FakeClock()
    async with build(handler, clock) as fetcher:
        result = await fetcher.get("example.com", "/products.json")
    assert result is not None
    assert fetcher.pii_domains == set()


async def test_a_homepage_with_contact_addresses_is_still_usable():
    """HTML contact info is public; discarding it would cost platform detection."""
    html = "<html>" + "".join(f"<a href='mailto:p{i}@shop.com'>x</a>" for i in range(9)) + "</html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    clock = FakeClock()
    async with build(handler, clock) as fetcher:
        result = await fetcher.get("example.com", "/")
    assert result is not None
    assert fetcher.pii_domains == set()


async def test_server_error_on_robots_is_treated_as_disallow_all():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(500)
        return httpx.Response(200, text="should never be reached")

    clock = FakeClock()
    async with build(handler, clock) as fetcher:
        assert await fetcher.get("example.com", "/") is None
