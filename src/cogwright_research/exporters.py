"""Stage 7 — the four deliverables (CW-RES-001 §11).

Exports read from SQLite rather than from an in-memory run, so they can be regenerated
after a resumed or partial crawl without re-fetching anything.

Everything written here stays local and out of git (§9, .gitignore).
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .catalog import ProductRecord
from .quality import QualitySignals, pitch_line, top_defect_stats
from .scoring import TIER_TARGETS

# The first three sales calls (§11.4). Matched on registrable domain; if a shop is not in
# the crawl its dossier says so rather than inventing numbers.
DESIGN_PARTNERS = {
    "tcoflyfishing.com": "TCO Fly Shop",
    "schultzoutfitters.com": "Schultz Outfitters",
    "charliesflyboxinc.com": "Charlie's Fly Box",
}
DESIGN_PARTNER_ALIASES = {
    "charliesflybox.com": "Charlie's Fly Box",
    "tcoflyshop.com": "TCO Fly Shop",
}


def export_all(db_path: Path, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = _load_rows(conn)
        written = {
            "prospects": _write_prospects(rows, out_dir / "prospects.xlsx"),
            "summary": _write_summary(conn, rows, out_dir / "market_summary.md"),
            "vendor_gap": _write_vendor_gap(conn, out_dir / "vendor_gap.csv"),
            "dossiers": _write_dossiers(conn, rows, out_dir / "design_partner_dossiers.md"),
        }
    finally:
        conn.close()
    return written


def _load_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT d.domain, d.root_url, d.source_locators, d.excluded, d.exclusion_reason,
               c.platform, c.pos_platform, c.has_cart, c.catalog_size, c.variant_count,
               c.catalog_method, c.truncated, c.days_since_last_product_update, c.dormant,
               c.http_status, c.robots_blocked,
               q.pct_no_image, q.pct_no_product_type, q.pct_untagged,
               q.pct_default_variant_only, q.pct_thin_title, q.duplicate_title_count,
               q.pct_zero_price, q.pct_no_sku, q.defect_density,
               s.tier, s.fit_score, s.size_pts, s.platform_pts, s.vendor_pts,
               s.defect_pts, s.modifiers
        FROM domains d
        LEFT JOIN crawl_results  c ON c.domain = d.domain
        LEFT JOIN quality_signals q ON q.domain = d.domain
        LEFT JOIN scores         s ON s.domain = d.domain
        """
    )
    rows = [dict(r) for r in cur.fetchall()]

    overlap = _vendor_overlap_by_domain(conn)
    for row in rows:
        stats = overlap.get(row["domain"], {})
        row["vendor_overlap_pct"] = stats.get("pct")
        row["vendor_overlap_weighted"] = stats.get("weighted")
        row["distinct_vendors"] = stats.get("distinct")
        row["locator_count"] = len(_locators(row))
        row["pitch_line"] = pitch_line(_signals(row), row.get("catalog_size") or 0)
    return rows


def _vendor_overlap_by_domain(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    cur = conn.execute(
        "SELECT domain, vendor_canonical, product_count FROM vendor_matches"
    )
    totals: dict[str, list[tuple[str | None, int]]] = defaultdict(list)
    for row in cur.fetchall():
        totals[row["domain"]].append((row["vendor_canonical"], row["product_count"] or 0))

    out: dict[str, dict[str, float]] = {}
    for domain, entries in totals.items():
        if not entries:
            continue
        matched = [e for e in entries if e[0]]
        product_total = sum(count for _, count in entries) or 1
        out[domain] = {
            "pct": round(len(matched) / len(entries), 4),
            "weighted": round(sum(c for _, c in matched) / product_total, 4),
            "distinct": len(entries),
        }
    return out


def _write_prospects(rows: list[dict[str, Any]], path: Path) -> Path:
    """One row per domain, sorted by fit_score desc (§11.1)."""
    columns = [
        "domain", "tier", "tier_target", "fit_score", "platform", "pos_platform",
        "catalog_size", "variant_count", "catalog_method", "truncated",
        "vendor_overlap_pct", "vendor_overlap_weighted", "distinct_vendors",
        "defect_density", "pct_no_sku", "pct_no_product_type", "pct_no_image",
        "days_since_last_product_update", "dormant", "locator_count", "source_locators",
        "has_cart", "http_status", "excluded", "exclusion_reason", "pitch_line",
    ]
    prepared = []
    for row in rows:
        record = dict(row)
        record["tier_target"] = TIER_TARGETS.get(row.get("tier") or "", "")
        record["source_locators"] = ", ".join(_locators(row))
        prepared.append({col: record.get(col) for col in columns})

    frame = pd.DataFrame(prepared, columns=columns)
    # SKU count is the primary rank; variant count breaks ties (§8).
    frame = frame.sort_values(
        by=["fit_score", "catalog_size", "variant_count"],
        ascending=False,
        na_position="last",
    )
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="prospects")
        sheet = writer.sheets["prospects"]
        sheet.freeze_panes = "A2"
        for idx, col in enumerate(columns, start=1):
            width = max(len(col) + 2, 14)
            sheet.column_dimensions[sheet.cell(row=1, column=idx).column_letter].width = min(width, 46)
    return path


def _write_summary(conn: sqlite3.Connection, rows: list[dict[str, Any]], path: Path) -> Path:
    attempted = [r for r in rows if r.get("http_status") is not None or r.get("platform")]
    classified = [r for r in rows if (r.get("platform") or "unknown") != "unknown"]
    sized = [r for r in rows if r.get("catalog_size") is not None]
    # §5: a dormant site is a failing business, not a prospect, however large its
    # catalog. It stays in the tier tables but is not part of the serviceable market.
    outreach = [
        r for r in rows
        if r.get("tier") in {"A", "B", "C"}
        and not r.get("excluded")
        and r.get("has_cart")
        and not r.get("dormant")
    ]
    dormant_in_band = [
        r for r in rows if r.get("tier") in {"A", "B", "C"} and r.get("dormant")
    ]

    tier_counts = Counter(r.get("tier") for r in rows if r.get("tier"))
    platform_counts = Counter((r.get("platform") or "unknown") for r in rows)
    log_counts = dict(
        conn.execute("SELECT outcome, COUNT(*) FROM request_log GROUP BY outcome").fetchall()
    )

    lines = [
        "# Fly shop market summary (CW-RES-001)",
        "",
        f"Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}.",
        "",
        "## Population",
        "",
        f"- Domains in universe: **{len(rows)}**",
        f"- Attempted: **{len(attempted)}**",
        f"- Classified by platform: **{len(classified)}**",
        f"- Catalog size determined: **{len(sized)}**",
        f"- Dormant (no product update in 180+ days): **{sum(1 for r in rows if r.get('dormant'))}**",
        "",
        "## Serviceable available market",
        "",
        "The modeled figure this replaces was 300-400 shops in the serviceable band.",
        "",
        f"- **Measured SAM (tiers A-C, live cart, not dormant): {len(outreach)} shops.**",
        "",
        "This is a floor, not an estimate: it counts only shops whose catalog size was",
        "actually determined from public data. Domains in the `undetermined` bucket are",
        "unclassified, not disqualified.",
        "",
        _dormant_note(len(dormant_in_band)),
        "They are excluded from the figure above on purpose: a large catalog that has",
        "stopped moving is a failing business, not a prospect.",
        "",
        "## By tier",
        "",
        "| Tier | Shops | Median SKUs | Target |",
        "|---|---:|---:|---|",
    ]
    for tier in ("A", "B", "C", "D"):
        members = [r for r in rows if r.get("tier") == tier and r.get("catalog_size")]
        median = int(statistics.median([r["catalog_size"] for r in members])) if members else 0
        lines.append(
            f"| {tier} | {tier_counts.get(tier, 0)} | {median:,} | {TIER_TARGETS[tier]} |"
        )

    lines += ["", "## By platform", "", "| Platform | Shops | Median SKUs |", "|---|---:|---:|"]
    for platform, count in platform_counts.most_common():
        members = [r for r in rows if (r.get("platform") or "unknown") == platform and r.get("catalog_size")]
        median = int(statistics.median([r["catalog_size"] for r in members])) if members else 0
        lines.append(f"| {platform} | {count} | {median:,} |")

    lines += [
        "",
        "## Point of sale",
        "",
        "`pos_platform` is `unknown` for every row, by design. Lightspeed Retail and its",
        "peers are counter systems and are invisible from the public web; a shop can run",
        "Lightspeed at the register and Shopify online. The platform distribution above is",
        "evidence about **web** platforms and about nothing else. It is not evidence that",
        "POS integration is unnecessary. Treat POS as a discovery question on the call.",
        "",
        "## Crawl log",
        "",
    ]
    for outcome, count in sorted(log_counts.items()):
        lines.append(f"- `{outcome}`: {count}")
    lines += [
        "",
        f"- robots.txt violations: **{log_counts.get('violation', 0)}**",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _dormant_note(count: int) -> str:
    verb = "shop sits" if count == 1 else "shops sit"
    have = "has" if count == 1 else "have"
    return f"A further **{count}** {verb} in tiers A-C but {have} not updated a product in 180+ days."


def _write_vendor_gap(conn: sqlite3.Connection, path: Path) -> Path:
    """Unmatched vendors ranked by prospect count (§11.3) — the mapping-library roadmap."""
    cur = conn.execute(
        """
        SELECT vendor_raw, COUNT(DISTINCT domain) AS prospect_count,
               SUM(product_count) AS total_products
        FROM vendor_matches
        WHERE vendor_canonical IS NULL
        GROUP BY LOWER(vendor_raw)
        ORDER BY prospect_count DESC, total_products DESC
        """
    )
    frame = pd.DataFrame(
        [dict(r) for r in cur.fetchall()],
        columns=["vendor_raw", "prospect_count", "total_products"],
    )
    frame.to_csv(path, index=False)
    return path


def _write_dossiers(conn: sqlite3.Connection, rows: list[dict[str, Any]], path: Path) -> Path:
    by_domain = {r["domain"]: r for r in rows}
    lines = [
        "# Design partner dossiers (CW-RES-001 §11.4)",
        "",
        "Prep sheets for the first three sales calls.",
        "",
    ]
    targets = {**DESIGN_PARTNERS, **DESIGN_PARTNER_ALIASES}
    seen_names: set[str] = set()

    for domain, name in targets.items():
        if name in seen_names:
            continue
        row = by_domain.get(domain)
        if row is None:
            continue
        seen_names.add(name)
        lines += _dossier_section(conn, name, row)

    for name in sorted(set(DESIGN_PARTNERS.values()) - seen_names):
        lines += [
            f"## {name}",
            "",
            "Not present in this crawl — the domain was never reached, or its catalog size",
            "could not be determined from public data. No figures are shown rather than",
            "estimated ones.",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _dossier_section(conn: sqlite3.Connection, name: str, row: dict[str, Any]) -> list[str]:
    vendors = conn.execute(
        """
        SELECT vendor_raw, vendor_canonical, match_type, product_count
        FROM vendor_matches WHERE domain=?
        ORDER BY product_count DESC LIMIT 12
        """,
        (row["domain"],),
    ).fetchall()

    total = row.get("catalog_size") or 0
    stats = top_defect_stats(_signals(row), total, limit=5)
    overlap = row.get("vendor_overlap_weighted")

    variants = f", {row['variant_count']:,} variants" if row.get("variant_count") else ""
    tier = row.get("tier") or ""
    overlap_text = f"{overlap:.0%}" if overlap is not None else "n/a"
    density = row.get("defect_density")
    density_text = f"{density:.2f}" if density is not None else "n/a"
    fit = row.get("fit_score")

    section = [
        f"## {name}",
        "",
        f"- **Domain:** {row['domain']}",
        f"- **Platform:** {row.get('platform') or 'unknown'} "
        f"(POS: {row.get('pos_platform') or 'unknown'} — ask on the call)",
        f"- **Catalog:** {total:,} products{variants} "
        f"(via {row.get('catalog_method') or 'undetermined'})",
        f"- **Tier:** {tier or 'n/a'} — {TIER_TARGETS.get(tier, 'n/a')}",
        f"- **Fit score:** {fit if fit is not None else 'n/a'}",
        f"- **Vendor overlap (weighted):** {overlap_text} across "
        f"{row.get('distinct_vendors') or 0} distinct vendors",
        f"- **Defect density:** {density_text}",
        "",
        "### Top five defect stats",
        "",
    ]
    section += [f"{i}. {stat}" for i, stat in enumerate(stats, start=1)] or ["_None computed._"]
    section += ["", "### Vendor mix (top by product count)", ""]
    if vendors:
        section += ["| Vendor | Mapped to | Match | Products |", "|---|---|---|---:|"]
        for v in vendors:
            section.append(
                f"| {v['vendor_raw']} | {v['vendor_canonical'] or '—'} | "
                f"{v['match_type']} | {v['product_count']:,} |"
            )
    else:
        section.append("_No vendor data (non-Shopify or products.json unavailable)._")
    section.append("")
    return section


def _locators(row: dict[str, Any]) -> list[str]:
    raw = row.get("source_locators")
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _signals(row: dict[str, Any]) -> QualitySignals:
    return QualitySignals(
        pct_no_image=row.get("pct_no_image") or 0.0,
        pct_no_product_type=row.get("pct_no_product_type") or 0.0,
        pct_untagged=row.get("pct_untagged") or 0.0,
        pct_default_variant_only=row.get("pct_default_variant_only") or 0.0,
        pct_thin_title=row.get("pct_thin_title") or 0.0,
        duplicate_title_count=row.get("duplicate_title_count") or 0,
        pct_zero_price=row.get("pct_zero_price") or 0.0,
        pct_no_sku=row.get("pct_no_sku") or 0.0,
        defect_density=row.get("defect_density") or 0.0,
    )


__all__ = ["ProductRecord", "export_all"]
