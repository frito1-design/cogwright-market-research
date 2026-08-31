# Packing-slip SKU matching — diagnosis and fix

Work against the brief of 2026-08-31 ("N items on packing lists aren't on their POs").
Target system is **Hook / mini-PIM**, Supabase project `zdffjldqsckiqnuxlsni`, tenant
`6308eb4f-716f-4309-8ad2-218c6c98ab0f` — not this repository. The SQL and the edge-function
patch live here because this is the branch the work was assigned to; **nothing here has been
applied to Hook.** See *Deploying* below.

## §7 — where the job is

`notify_off_po_skus`, a Supabase **edge function** (id `707a00a2-…`, v19, `verify_jwt=false`
so pg_cron can call it). Added after the 2026-07-28 inventory, by migration
`20260820162351 off_po_sku_alerts`; the cron is `20260820164542 off_po_sku_alert_cron`. Not
an n8n workflow — an n8n search for the alert returned nothing.

The function does **no matching of its own**. All detection is SQL:

```
sweep_po_offdoc_skus(tenant)   -- gating, alert upsert/retire, what to announce
  └─ po_offdoc_skus(po_id)     -- what the document says vs what the PO says
       └─ catalog_sku_match()  -- upc → exact → punct → prefix → similar
```

`catalog_sku_match` is where the change belongs. Extraction is untouched, as the brief asked.

## What was actually wrong

The brief's §1 — "compares extracted vendor SKU strings literally, with no normalization and
no fallback" — **is out of date.** A five-tier matcher already exists, with homoglyph folding,
UPC matching, punctuation-insensitive matching and trigram similarity, all ambiguity-declining.
It was built 2026-08-20 → 08-22.

The real defect is narrower and much sharper:

> **The `prefix` tier prefix-matches on `normalize_vendor_sku()`, which retains the trailing
> truncation artifact.** `AIR2-906-...` is compared literally, dots included, so no catalogue
> SKU can ever start with it. The one tier that exists to resolve truncated SKUs is the one
> tier truncation defeats.

Measured against the live catalogue — prefix candidates found, by key:

| Printed SKU | on `normalize_vendor_sku` (today) | on the match key |
|---|---|---|
| `AIR2-906-...` | **0** | 1 |
| `PARADIGM...` | **0** | 4 |
| `ECHOBASE-SPOOL...` | **0** | 3 |
| `AVANTTII90...` | **0** | 2 |

Same root cause reaches the clipping case: `HDN302SPR1:` never reaches the prefix tier either.
Two smaller gaps sit beside it — nothing folds `O`/`0` (`WPTO` for `WPT0`), and an ambiguous
result is reported to the warehouse as *"no match in Shopify — check before creating it"*,
which is the expensive half of the bug. It is in Shopify; we just could not say which one.

## Corrections to the brief

1. **§1 is stale** — normalization and fallback already exist (above).
2. **§5's "Dr. Slick hand patch (2026-08-21) — remove it as part of this change" — there is no
   such patch.** The generic rule (`20260822144252 match_skus_across_punctuation`) already
   absorbs it. Verified live: `EBOB4→E-BOB4`, `COMBO5G→COMBO-5G`, `ERIBO→E-RIBO`, `EC5→E-C5`
   all return `punct` today. The `none` rows still visible on PO-10408-WH are stale history on
   a closed PO, not current behaviour. §8 check 4 already passes; nothing to remove.
3. **§4.2 Constraint 3's denylist is unnecessary.** The two same-vendor pairs (Renzetti
   `SC2302-R`/`SC2302R`, TroutHunter `BGET-016`/`BGET016`) tie under vendor scoping and are
   already declined by the existing count test. A denylist table would be dead code, so none is
   added — the invariant is asserted by test instead.
4. **§6c is wrong as generalised.** `line_confidence` is populated on **30,817 of 57,568**
   `email_line_items` (53.5%). It is null on all 660 lines of invoice 298112, so the
   observation holds for that extractor — but "nothing is filling it" does not.

The brief's collision arithmetic, by contrast, **reproduces exactly**: 24 keys/48 SKUs naive →
18/36 preserving `/` → 2/4 adding vendor scope, and the six slash pairs are precisely C2441,
C61S, FM6045, FM8010, HR410, SL53U.

## The change

- `001_sku_match_tiers.sql` — adds `sku_match_key` (slash-preserving), `sku_stem_key`
  (truncation artifact removed), `sku_desc_size_token`; rebuilds `catalog_sku_match` with the
  prefix tier keyed on the stem, plus `prefix_desc` (description size tiebreak), `ozero`
  (0/O), and `ambiguous` (found candidates, declined to choose). Returns `candidate_count` and
  `candidates`.
- `002_offdoc_carry_candidates.sql` — carries both through `po_offdoc_skus` and
  `sweep_po_offdoc_skus` onto `po_offdoc_sku_alerts` (which *is* the §4.5 log — it already has
  the raw SKU, matched SKU, rule and a `resolved_at`; it only lacked the evidence).
- `deployed/` — the edge function as shipped (`index.ts` plus its `_shared/notify.ts`
  dependency), carrying the three new kinds in `statusText`. This was not optional: the
  function's own comment notes that an unlisted kind falls through to "create it", so the SQL
  without it would have made the alert worse. Kept as the whole file rather than a patch
  fragment so there is one copy to drift from.
- `004_suppress_lines_resolved_onto_the_po.sql` — the other half of the false positive, found
  during deployment. `doc_on_po` compares the *printed* SKU against the PO and never consults
  the match, so a line resolved to `HDN302SPR12` still counted as "not on the PO" even though
  the PO carried `HDN302SPR12` at the same quantity. Without this, §8 check 3 fails: the four
  SKUs still produce alert lines, merely better-labelled ones. Only a *resolved* match
  suppresses — `ambiguous` has no `matched_sku`, and a genuinely new SKU can never be
  suppressed at all.

Two properties hold throughout:

- **Strictly additive.** Every new tier fires only where the function returns nothing today,
  and every one declines on ambiguity. No input that matches today changes its answer.
- **Constraint 1 is a guard, not a key swap.** Candidate *generation* still uses the legacy
  slash-stripping squeeze. Preserving the slash there is the tempting move and the unsafe one:
  it resolves `C2441-1-0` onto the size 10 hook. Instead a slash anywhere in an ambiguous
  candidate set forces a decline.

`in_shopify` also stops being derived from `match_kind IS NOT NULL` — with an `ambiguous` kind
in play that would have re-created the original wrong instruction.

## Verification

`verify_before_deploy.sql` is read-only and self-contained: the new keys are inlined as
expressions, so it proves the tiers on live production data **before** the migration. Run it
again afterwards; every row must still say PASS. Current result — 9/9 PASS:

| Test | Result |
|---|---|
| `HDN302SPR1:` / `.` / `(` + description size → `HDN302SPR12` / `14` / `16` | PASS (`prefix_desc`) |
| `WPTO` → `WPT0` | PASS (`ozero`) |
| `HDN302SPR1` clean truncation + size 16 → `HDN302SPR16` | PASS (`prefix_desc`) |
| `EBOB4` → `E-BOB4`, `COMBO5G` → `COMBO-5G` (regression) | PASS (`punct`) |
| `HDN302SPR1:` with no size in the description | PASS — declines, names all 3 |
| `C2441-1/0` (Constraint 1) | PASS — declines, names `C24411/0` and `C244110` |

That covers §8 checks 1–4. Checks 5 and 6 (the 2,990 genuinely-new SKUs must still alert; the
log must be inspectable) need a post-deploy `dry_run` sweep — see below.

## Deployment status

**001, 002 and 004 are APPLIED to production** (migrations
`sku_match_tiers_stem_desc_ozero`, `offdoc_carry_match_candidates`,
`offdoc_suppress_lines_resolved_onto_the_po`). Verified live: 10/10 on the matcher, and a
`dry_run` sweep returns `pending: 0` with all five PO-10458-WH / PO-10460-JM rows retired,
each carrying its matched SKU and candidate set.

**The edge function is deployed** — `notify_off_po_skus` v20, ACTIVE, `verify_jwt` still
`false` so pg_cron can keep calling it with the sweep token. A post-deploy `dry_run` returns
200 / `pending: 0`, which also confirms the function loads and the RPC's new shape matches.

Note for anyone reading the brief: the cron is `0 14-23 * * *` — **hourly, ten times a day**,
not the daily 09:00 the brief inferred from a single send.

All nine match kinds were driven through the shipped `statusText`; only `none` reaches the
default. The trigger case now reads *"in Shopify as HDN302SPR12 — truncated SKU, matched on
the size in the description (chosen from 3 similar SKUs — verify) — check the size, then add
the line"* (green) in place of *"no match in Shopify — check before creating it"* (red).

Rollback is the previous `catalog_sku_match` / `po_offdoc_skus` / `sweep_po_offdoc_skus`
bodies; the two added columns are nullable and can be left in place.

## Still open (brief §6, not addressed here)

`6a` missing SKU column on six vendors (946 values / 1,672 lines — roughly 4× this fix's
volume, and an extraction-mapping problem no matching rule reaches); `6b` ~471 duplicate SKU
rows in `catalog_items`; `6d` the fixed-width clipping in `extract_invoice_pdf`, which would
remove §3a at source. `6c` needs restating first — see correction 4.
