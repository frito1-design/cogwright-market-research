# Run status — what is done, what is not, and why

Written at build time. Update it after the first live crawl.

## The short version

The crawler is complete and tested. **It has not been run against a real site**, so there
is no market data yet — no `prospects.xlsx` with real shops in it, no measured SAM figure,
no vendor gap list. What exists is the instrument, verified against synthetic storefronts.

## Why no crawl happened

The session that built this had no outbound network access. The environment's egress proxy
denies every host that is not on its allowlist (package registries, GitHub, Anthropic):

```
$ curl https://www.tcoflyfishing.com/
curl: (56) CONNECT tunnel failed, response 403
```

The proxy's own status endpoint confirms it as a policy denial, not a transient failure.
This is a property of the build environment, not of the crawler. Run `cwresearch crawl`
anywhere with ordinary internet access and it will work.

The pipeline degrades correctly rather than inventing data — a crawl attempted here logs
`error` outcomes and records `catalog_method = 'undetermined'` with a null catalog size,
exactly as §5 requires for an undeterminable catalog.

## Acceptance criteria (§12)

| Criterion | Status |
|---|---|
| ≥ 400 domains attempted | **Pending a run.** The universe currently holds 86 unverified seed domains; the dealer-locator harvest is what reaches 600–900, and it needs network. |
| ≥ 250 classified by platform | **Pending a run.** |
| ≥ 150 with catalog size determined | **Pending a run.** |
| Zero robots.txt violations in the crawl log | **Enforced by construction and tested.** Disallowed paths are never issued; `violations()` re-audits the log against the rules. |
| Fuzzy matches ≥ 88 logged, none auto-accepted below | **Done.** Threshold enforced in `vendors_match.py`, every match written to `vendor_matches` with its score and `match_type`. |
| All four output files present, xlsx opens clean | **Done** against synthetic data — `scripts/demo_run.py` produces all four and the test suite reads the workbook back with pandas. |
| Full run under 8 hours wall clock | **Pending a run.** At 2s/domain serial and 8 concurrent, ~500 domains at ~10 requests each is roughly 20 minutes of request time; sitemap-heavy shops dominate the tail. Comfortable, but unmeasured. |
| Re-running is idempotent | **Done and tested.** Every write is an upsert on the natural key; `test_rerunning_is_idempotent` crawls twice and asserts no duplicate rows. |

## Before the first live run, do these three things

**1. Verify the dealer locator URLs.** Every entry in `reference/dealer_locators.yaml` is
`verified: false`. They were written from the brand list in §3 without network access to
confirm them. Several are certainly wrong, and several more will be JavaScript front-ends
over a JSON endpoint — open each in a browser, fix the URL, set `kind: json` with
`array_path`/`website_key` where needed, and flip `verified: true`. This is the single
highest-leverage hour of work on the project: Stage 1 is the input to everything else, and
the locator harvest is what makes vendor overlap meaningful.

**2. Treat `seeds/domains_seed.csv` as disposable.** Its 86 domains were written from
recall and none has been confirmed to resolve. It exists to get the pipeline moving on day
one. It is safe only because Stage 2 validates everything — a domain that does not resolve
or shows no e-commerce signal is excluded automatically, and a wrong guess costs one HTTP
request. Expect meaningful attrition. Do not report anything from it as market data.

**3. Check the AFFTA line.** `dealer_locators.yaml` deliberately contains no AFFTA entry.
§3 permits only a publicly published member directory, visible to an anonymous browser.
Nothing obtained through the advisory relationship goes in this repo. If a public
directory exists, verify it while logged out before adding it.

## Known limits of the instrument

- **POS is invisible.** `pos_platform` is `unknown` on every row by design. See the README.
- **Non-Shopify shops get no quality signals.** The sitemap path yields a URL count and a
  `lastmod`, nothing to inspect, so §7 defect signals and §6 vendor overlap are blank for
  them. Their fit score is built from size and platform alone and is correspondingly less
  informative — not lower, less certain.
- **The `undetermined` bucket is not the same as "small".** A shop with no sitemap and a
  disabled `/products.json` gets `catalog_size = null`, no tier, and no score. It is
  unclassified, not disqualified, and it needs a human look rather than exclusion.
- **Guide/lodge exclusion is a keyword heuristic** and will occasionally catch a real shop
  (a name containing "lodge"). Excluded domains are retained in the `domains` table with
  their `exclusion_reason` rather than dropped, so a review pass can reinstate one by
  clearing the flag. "outfitters" is deliberately not an exclusion keyword — it is ordinary
  in legitimate shop names, one design partner included.
