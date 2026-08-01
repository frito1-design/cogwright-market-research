"""Command line entry point.

    cwresearch universe   # Stage 1  — build the domain list
    cwresearch crawl      # Stages 2-6
    cwresearch export     # Stage 7  — the four deliverables
    cwresearch run        # all of the above
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime

from . import db
from .config import Paths
from .exporters import export_all
from .fetcher import PoliteFetcher
from .pipeline import run_crawl
from .universe import Universe, harvest_locator, load_locator_specs
from .vendors_match import VendorLibrary

log = logging.getLogger("cwresearch")


async def cmd_universe(args: argparse.Namespace, paths: Paths) -> int:
    universe = Universe()
    seeded = universe.load_seed_csv(paths.seeds / "domains_seed.csv")
    log.info("seed file contributed %d domains", seeded)

    specs = load_locator_specs(paths.reference / "dealer_locators.yaml")
    if specs and not args.no_locators:
        unverified = [s.name for s in specs if not s.verified]
        if unverified:
            # Not fatal — an unverified locator still gets harvested — but a run that
            # returns few domains is usually a payload-shape problem, not a market fact.
            log.warning(
                "%d/%d locators are unverified (payload shape unconfirmed): %s",
                len(unverified), len(specs), ", ".join(unverified),
            )
        async with PoliteFetcher() as fetcher:
            for spec in specs:
                added = await harvest_locator(fetcher, spec, universe)
                flag = "" if spec.verified else "  [unverified]"
                log.info("locator %-22s +%d domains%s", spec.name, added, flag)
    elif not specs:
        log.warning("no dealer_locators.yaml entries; universe is seed-only")

    now = datetime.now(UTC).isoformat()
    with db.connect(paths.db) as conn:
        for row in universe.to_rows(now):
            db.upsert_domain(conn, row)
            db.merge_domain_locators(conn, row["domain"], json.loads(row["source_locators"]))

    crawlable = universe.crawlable()
    log.info(
        "universe: %d candidates, %d crawlable, %d excluded",
        len(universe.candidates), len(crawlable), len(universe.candidates) - len(crawlable),
    )
    return 0


async def cmd_crawl(args: argparse.Namespace, paths: Paths) -> int:
    library = VendorLibrary.from_csv(paths.reference / "vendors.csv")
    with db.connect(paths.db) as conn:
        rows = conn.execute(
            "SELECT domain, source_locators FROM domains WHERE excluded=0 ORDER BY domain"
        ).fetchall()
    targets = [(r["domain"], len(json.loads(r["source_locators"] or "[]"))) for r in rows]
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        log.error("no crawlable domains; run `cwresearch universe` first")
        return 1

    log.info("crawling %d domains", len(targets))
    outcomes = await run_crawl(targets, library, paths=paths)
    classified = sum(1 for o in outcomes if o.platform != "unknown")
    sized = sum(1 for o in outcomes if o.catalog.catalog_size is not None)
    log.info("done: %d crawled, %d classified, %d sized", len(outcomes), classified, sized)
    return 0


async def cmd_export(args: argparse.Namespace, paths: Paths) -> int:
    if not paths.db.exists():
        log.error("no database at %s; run `cwresearch crawl` first", paths.db)
        return 1
    written = export_all(paths.db, paths.exports)
    for name, path in written.items():
        log.info("wrote %-12s %s", name, path)
    return 0


async def cmd_run(args: argparse.Namespace, paths: Paths) -> int:
    for step in (cmd_universe, cmd_crawl, cmd_export):
        code = await step(args, paths)
        if code:
            return code
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cwresearch", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_universe = sub.add_parser("universe", help="Stage 1: build the domain universe")
    p_universe.add_argument("--no-locators", action="store_true", help="seeds only")
    p_universe.set_defaults(func=cmd_universe)

    p_crawl = sub.add_parser("crawl", help="Stages 2-6")
    p_crawl.add_argument("--limit", type=int, default=0, help="crawl only the first N domains")
    p_crawl.set_defaults(func=cmd_crawl)

    p_export = sub.add_parser("export", help="Stage 7: write the four deliverables")
    p_export.set_defaults(func=cmd_export)

    p_run = sub.add_parser("run", help="universe + crawl + export")
    p_run.add_argument("--limit", type=int, default=0)
    p_run.add_argument("--no-locators", action="store_true")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    return asyncio.run(args.func(args, Paths()))


if __name__ == "__main__":
    sys.exit(main())
