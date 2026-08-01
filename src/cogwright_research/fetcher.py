"""Polite HTTP layer.

Every constraint in CW-RES-001 §9 lives here rather than in the callers, so a stage
cannot accidentally opt out of it. Callers get one method — `get` — and it is incapable
of issuing a request that violates robots.txt or the rate limit.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import CrawlPolicy
from .robots import RobotsRules, path_of

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Keys whose presence in a JSON payload means we have been handed customer, order or
# staff records. §9: discard, do not store, flag the domain.
PII_JSON_KEYS = frozenset(
    {
        "customers", "customer", "orders", "order", "checkouts", "checkout",
        "draft_orders", "users", "staff", "addresses", "billing_address",
        "shipping_address", "line_items", "transactions", "refunds",
    }
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
#: More than a handful of distinct addresses in one response is a list, not a contact link.
EMAIL_DUMP_THRESHOLD = 5


class DomainAbandoned(Exception):
    """Raised when a domain has exhausted its retry budget; stop crawling it."""


class RobotsDisallowed(Exception):
    """Raised when robots.txt forbids the path. Never surfaces as a request."""


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    text: str
    headers: dict[str, str]
    elapsed_ms: int
    tls_ok: bool = True

    def json(self) -> Any:
        import json

        return json.loads(self.text)


@dataclass
class RequestRecord:
    """One line of the audit trail required by §12."""

    domain: str
    url: str
    status: int | None
    outcome: str  # ok | robots_blocked | error | pii_discarded | abandoned
    note: str = ""


@dataclass
class _DomainState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_request: float = 0.0
    robots: RobotsRules | None = None
    abandoned: bool = False
    consecutive_throttles: int = 0


class PoliteFetcher:
    """Rate-limited, robots-obeying async HTTP client."""

    def __init__(
        self,
        policy: CrawlPolicy | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Any = time.monotonic,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.policy = policy or CrawlPolicy.from_env()
        self._states: dict[str, _DomainState] = {}
        self._semaphore = asyncio.Semaphore(self.policy.max_concurrent_domains)
        self._clock = clock
        self._sleep = sleep
        self.request_log: list[RequestRecord] = []
        self.pii_domains: set[str] = set()
        self._client = httpx.AsyncClient(
            headers={"User-Agent": self.policy.user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=self.policy.request_timeout,
            follow_redirects=True,
            transport=transport,
            # No proxy rotation, no browser impersonation (§9).
        )

    async def __aenter__(self) -> PoliteFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @asynccontextmanager
    async def crawl_domain(self, domain: str) -> AsyncIterator[PoliteFetcher]:
        """Hold one of the global domain slots for the duration of a domain's crawl."""
        async with self._semaphore:
            yield self

    def _state(self, domain: str) -> _DomainState:
        return self._states.setdefault(domain, _DomainState())

    async def _throttle(self, domain: str, state: _DomainState) -> None:
        """Enforce the larger of our floor and the site's declared Crawl-delay."""
        delay = self.policy.min_delay_per_domain
        if state.robots is not None:
            declared = state.robots.crawl_delay(self.policy.user_agent)
            if declared is not None:
                delay = max(delay, declared)
        waited = self._clock() - state.last_request
        if state.last_request and waited < delay:
            await self._sleep(delay - waited)
        state.last_request = self._clock()

    async def _load_robots(self, domain: str, scheme: str, state: _DomainState) -> RobotsRules:
        if state.robots is not None:
            return state.robots
        url = f"{scheme}://{domain}/robots.txt"
        await self._throttle(domain, state)
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            # Unreachable robots.txt is treated as restrictive-by-default only for
            # crawl-delay purposes; access itself stays allowed per RFC 9309 §2.3.1.
            log.debug("robots fetch failed for %s: %s", domain, exc)
            state.robots = RobotsRules.allow_all(fetch_failed=True)
            return state.robots

        if resp.status_code >= 500:
            # 5xx means "assume disallowed" under RFC 9309. Abandon politely.
            state.robots = RobotsRules.parse("User-agent: *\nDisallow: /")
        elif resp.status_code >= 400:
            state.robots = RobotsRules.allow_all(absent=True)
        else:
            state.robots = RobotsRules.parse(resp.text)
        return state.robots

    async def get(
        self,
        domain: str,
        path: str = "/",
        *,
        scheme: str = "https",
        allow_status: tuple[int, ...] = (),
    ) -> FetchResult | None:
        """Fetch one URL. Returns None when robots forbids it or the body was discarded.

        Raises DomainAbandoned once the domain's retry budget is spent.
        """
        state = self._state(domain)
        if state.abandoned:
            raise DomainAbandoned(domain)

        url = path if path.startswith("http") else f"{scheme}://{domain}{path}"
        robots = await self._load_robots(domain, scheme, state)
        if self.policy.obey_robots and not robots.can_fetch(self.policy.user_agent, path_of(url)):
            self.request_log.append(
                RequestRecord(domain, url, None, "robots_blocked", "disallowed by robots.txt")
            )
            return None

        backoff = 2.0
        for attempt in range(1, self.policy.max_retries + 1):
            await self._throttle(domain, state)
            started = self._clock()
            try:
                resp = await self._client.get(url)
            except httpx.HTTPError as exc:
                if attempt == self.policy.max_retries:
                    self.request_log.append(
                        RequestRecord(domain, url, None, "error", type(exc).__name__)
                    )
                    return None
                await self._sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code in RETRY_STATUSES and resp.status_code not in allow_status:
                state.consecutive_throttles += 1
                if state.consecutive_throttles >= self.policy.max_retries:
                    state.abandoned = True
                    self.request_log.append(
                        RequestRecord(domain, url, resp.status_code, "abandoned", "repeated 429/5xx")
                    )
                    raise DomainAbandoned(domain)
                await self._sleep(_retry_after(resp, backoff))
                backoff *= 2
                continue

            state.consecutive_throttles = 0
            body = resp.text
            if _looks_like_pii(body, resp.headers.get("content-type", "")):
                self.pii_domains.add(domain)
                self.request_log.append(
                    RequestRecord(domain, url, resp.status_code, "pii_discarded", "flagged for review")
                )
                return None  # body deliberately not returned or stored

            self.request_log.append(RequestRecord(domain, url, resp.status_code, "ok"))
            return FetchResult(
                url=url,
                final_url=str(resp.url),
                status=resp.status_code,
                text=body,
                headers={k.lower(): v for k, v in resp.headers.items()},
                elapsed_ms=int((self._clock() - started) * 1000),
            )
        return None

    def robots_for(self, domain: str) -> RobotsRules | None:
        return self._states.get(domain, _DomainState()).robots

    def violations(self) -> list[RequestRecord]:
        """Re-audit the log: any request we actually issued that robots.txt forbade.

        `get` refuses disallowed paths before issuing, so this is expected to be empty —
        but §12 asks for zero violations *in the crawl log*, which is a claim worth
        checking against the rules rather than asserting from control flow.
        """
        found: list[RequestRecord] = []
        for record in self.request_log:
            if record.outcome not in ("ok", "pii_discarded"):
                continue
            robots = self.robots_for(record.domain)
            if robots is None:
                continue
            if not robots.can_fetch(self.policy.user_agent, path_of(record.url)):
                found.append(record)
        return found


def _retry_after(resp: httpx.Response, fallback: float) -> float:
    raw = resp.headers.get("retry-after")
    if raw:
        try:
            return min(float(raw), 120.0)
        except ValueError:
            pass
    return fallback


def _looks_like_pii(body: str, content_type: str) -> bool:
    """Cheap guard against accidentally ingesting customer/order/staff records.

    The email-density check is applied to structured payloads only. A shop's homepage
    listing a few staff addresses is ordinary public contact information, and we retain
    no page text from it — discarding those would cost platform detection on a large
    share of the universe for no privacy gain.
    """
    ctype = content_type.lower()
    structured = "json" in ctype or "xml" in ctype
    if structured and len(set(EMAIL_RE.findall(body))) >= EMAIL_DUMP_THRESHOLD:
        return True
    if "json" not in ctype:
        return False
    import json

    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return False
    return _json_has_pii_keys(payload)


def _json_has_pii_keys(payload: Any, depth: int = 0) -> bool:
    if depth > 3:
        return False
    if isinstance(payload, dict):
        if PII_JSON_KEYS & {str(k).lower() for k in payload}:
            return True
        return any(_json_has_pii_keys(v, depth + 1) for v in payload.values())
    if isinstance(payload, list):
        return any(_json_has_pii_keys(v, depth + 1) for v in payload[:5])
    return False
