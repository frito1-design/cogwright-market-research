"""robots.txt parsing.

`urllib.robotparser` is deliberately not used: it silently returns `crawl_delay=None`
for the wildcard group in some layouts and its record selection is hard to assert on.
CW-RES-001 §9 requires zero violations in the crawl log, so the rules that gate every
request are implemented here where they can be unit tested directly.

Semantics follow the de-facto standard (RFC 9309):
  * the most specific matching User-agent group wins; `*` is the fallback
  * within a group, the longest matching path pattern wins
  * on an equal-length tie, Allow beats Disallow
  * an empty Disallow value means "allow everything"
  * `*` and `$` wildcards are honoured in paths
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, pattern)
    crawl_delay: float | None = None


@dataclass
class RobotsRules:
    """Evaluated robots.txt for one origin."""

    groups: list[_Group] = field(default_factory=list)
    fetch_failed: bool = False
    #: True when robots.txt returned 4xx. Per RFC 9309 that means unrestricted access.
    absent: bool = False

    @classmethod
    def parse(cls, text: str) -> RobotsRules:
        groups: list[_Group] = []
        current: _Group | None = None
        # A blank line or a directive line ends an agent block; consecutive User-agent
        # lines share one group.
        expecting_agents = False

        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            field_name, _, value = line.partition(":")
            key = field_name.strip().lower()
            value = value.strip()

            if key == "user-agent":
                if current is None or not expecting_agents:
                    current = _Group()
                    groups.append(current)
                    expecting_agents = True
                current.agents.append(value.lower())
                continue

            if current is None:
                continue  # directive before any User-agent: ignore
            expecting_agents = False

            if key == "disallow":
                # "Disallow:" with no value is an explicit allow-all; skip it so it
                # cannot out-rank a real rule via longest-match.
                if value:
                    current.rules.append((False, value))
            elif key == "allow":
                if value:
                    current.rules.append((True, value))
            elif key == "crawl-delay":
                with contextlib.suppress(ValueError):
                    current.crawl_delay = float(value)

        return cls(groups=groups)

    @classmethod
    def allow_all(cls, *, fetch_failed: bool = False, absent: bool = False) -> RobotsRules:
        return cls(groups=[], fetch_failed=fetch_failed, absent=absent)

    def _group_for(self, user_agent: str) -> _Group | None:
        ua = user_agent.lower()
        best: _Group | None = None
        best_len = -1
        wildcard: _Group | None = None
        for group in self.groups:
            for agent in group.agents:
                if agent == "*":
                    if wildcard is None:
                        wildcard = group
                elif agent in ua and len(agent) > best_len:
                    best, best_len = group, len(agent)
        return best or wildcard

    def can_fetch(self, user_agent: str, path: str) -> bool:
        group = self._group_for(user_agent)
        if group is None:
            return True
        target = unquote(path or "/")
        decision: bool | None = None
        decision_len = -1
        for allow, pattern in group.rules:
            if not _matches(pattern, target):
                continue
            length = len(pattern)
            # Longest match wins; Allow breaks a tie.
            if length > decision_len or (length == decision_len and allow):
                decision, decision_len = allow, length
        return True if decision is None else decision

    def crawl_delay(self, user_agent: str) -> float | None:
        group = self._group_for(user_agent)
        return group.crawl_delay if group else None


def _matches(pattern: str, path: str) -> bool:
    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in unquote(body))
    return re.match(regex + ("$" if anchored_end else ""), path) is not None


def path_of(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    return f"{path}?{parsed.query}" if parsed.query else path
