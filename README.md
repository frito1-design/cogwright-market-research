# Cogwright market research — fly shop intelligence crawler

Implements **CW-RES-001**. Replaces the modeled "roughly 300–400 shops in the serviceable
band" with a scored list of named prospects, built from public data, ranked by catalog
size, platform, vendor overlap with the Hook mapping library, and observable catalog decay.

Its second job matters more than the first: because the crawl computes catalog quality
signals from public data, each prospect row carries a `pitch_line` — the exact defect stat
that opens the cold email. The research output *is* the outbound campaign.

> **This directory is meant to live in its own repository.** CW-RES-001 §1 specifies
> `frito1-design/cogwright-market-research`. The session that wrote it could not create
> that repo — the GitHub app returned `403 Resource not accessible by integration` — so it
> landed here instead. It is entirely self-contained: create the repo and move this
> directory to its root, unchanged. Nothing here imports from or writes to the surrounding
> project.

## Status: built, not yet run

Every stage is implemented and tested. **No live crawl has been performed**, because the
environment that built it has no outbound network access — the egress proxy denies all
non-allowlisted hosts. See [`docs/RUN_STATUS.md`](docs/RUN_STATUS.md) for exactly which
acceptance criteria are met, which are pending a run, and what to check first.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

## Usage

```bash
cwresearch universe        # Stage 1 — dealer locators + seed list -> domains table
cwresearch crawl           # Stages 2-6 — platform, catalog, vendors, quality, score
cwresearch export          # Stage 7 — the four deliverables
cwresearch run             # all three

cwresearch crawl --limit 20        # start small
cwresearch universe --no-locators  # seeds only, no network
```

To see the output shape before committing to a multi-hour run:

```bash
python scripts/demo_run.py      # synthetic shops -> exports/demo/*
```

## How it works

| Stage | Module | What it does |
|---|---|---|
| 1 | `universe.py` | Harvests brand dealer locators + seed CSV, normalizes to registrable domains, dedupes, records `source_locators` |
| 2 | `platform_detect.py` | Classifies the web platform from homepage, headers and robots.txt |
| 3 | `catalog.py` | Shopify `/products.json` pagination; sitemap URL counting for everything else |
| 4 | `vendors_match.py` | Matches shop vendor strings against the Hook library: exact → alias → fuzzy |
| 5 | `quality.py` | Eight defect signals plus a 0–1 `defect_density`, from Stage 3 data only |
| 6 | `scoring.py` | Tier assignment and the 0–100 fit score with modifiers |
| 7 | `exporters.py` | `prospects.xlsx`, `market_summary.md`, `vendor_gap.csv`, `design_partner_dossiers.md` |

### What POS detection does not tell you

`pos_platform` is `unknown` on every row and always will be. Lightspeed Retail (R-Series)
and its peers are counter systems, invisible from the public web; a shop can run
Lightspeed at the register and Shopify online. When the results come back Shopify-heavy,
that is evidence about **web** platforms and about nothing else. It is not evidence that
Lightspeed integration is unnecessary. POS is a discovery question on the sales call. The
summary export repeats this warning where the distribution is printed, because that is
where someone will misread it.

### Two judgement calls worth knowing about

**Fuzzy vendor matching is deliberately restricted.** The Hook vendor list contains
single-word canonicals — `Peak`, `Surface`, `Alpine`, `Coal`, `Smith` — that collide with
ordinary product copy. A `token_set_ratio` of 88 against "Peak Design" is trivially
reached, and accepting it would inflate every overlap figure in the report. Fuzzy matching
is therefore withheld from canonicals under 8 characters that are a single token; those
must match exactly or through a registered alias. Every fuzzy hit is written to
`vendor_matches` with its score for review, and none is accepted below 88.

**Dormant shops are excluded from the SAM figure.** A site with 13,500 SKUs that has not
updated a product in 180 days is a failing business, not a prospect. It stays visible in
the tier tables, and it is counted separately in the summary, but it is not part of the
serviceable market.

## Crawl ethics (§9) — enforced, not configured

These live in `fetcher.py`, not in settings, so no stage can opt out:

- **robots.txt is parsed and obeyed**, including `Crawl-delay`. Disallowed paths are never
  requested — `get()` returns before issuing. `violations()` re-audits the whole log
  against the rules afterwards rather than asserting emptiness from control flow.
- **One request per 2 seconds per domain**, or the site's declared `Crawl-delay` if it is
  slower. `CrawlPolicy.from_env` clamps operator input in the polite direction only: you
  can make the crawl slower, never faster.
- **Global concurrency ≤ 8 domains.**
- **Honest User-Agent** with a contact URL:
  `Cogwright-Research/1.0 (+https://cogwright.com/research; contact@cogwright.com)`
- **Public, unauthenticated endpoints only.** No login walls, no checkout flows, no cart
  manipulation, no headless-browser evasion, no proxy rotation. A site that blocks the
  crawler stays blocked.
- **Zero PII.** Responses carrying customer, order or staff records are discarded
  unstored and the domain is logged for manual review.
- **Backoff on 429/503**, then the domain is abandoned entirely.
- **Nothing crawled is committed.** `data/` and `exports/` are gitignored.

This crawl points at businesses that will shortly receive a sales email. Sloppiness here
is not a technical problem — it is the end of the go-to-market motion.

## Vendor reference (§2)

`reference/vendors.csv` — 188 canonical vendors, 284 rows with aliases, pulled from the
Hook project (`zdffjldqsckiqnuxlsni`, tenant `6308eb4f…`). **Names and aliases only.** No
costs, no terms, no order history, no SKU-level data. The same firewall rule that applies
to client data, applied to Fly Fish Food's own.

Bookkeeping rows that are not merchandise brands (`amazon`, `vendor-unknown`,
`Flies - Vendors`, the Fly Fish Food house brands, `WhitingOLD`, the SaaS entries) are kept
in the CSV for fidelity with the source but ignored for overlap scoring — see
`NON_BRAND_CANONICALS` in `config.py`.

## Out of scope (§13)

No individual product-page fetching. No pricing intelligence or competitive price
comparison. No contact or email harvesting. No social, review, or traffic estimation. No
POS detection beyond the web surface. No writes to any Cogwright, Hook, or FlyAI system —
this repo reads `vendors`/`po_vendor_aliases` once, at setup, and never writes anywhere.

## Tests

```bash
pytest          # 67 tests, no network
ruff check src tests scripts
```

The suite covers robots semantics, the rate limiter (with a fake clock), the PII guard,
every platform signature, both catalog paths, vendor matching including the collision
guard, all eight quality signals, the scoring curve and modifiers, idempotent re-runs, and
an end-to-end run producing all four deliverables from synthetic storefronts.
