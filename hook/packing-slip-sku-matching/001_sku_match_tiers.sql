-- Packing-slip SKU matching — normalized fallback tiers for catalog_sku_match().
--
-- Target: Hook / mini-PIM, Supabase project zdffjldqsckiqnuxlsni.
-- Fixes the false "no match in Shopify — check before creating it" line in the
-- notify_off_po_skus alert, which tells the warehouse to create products we
-- already stock. PO-10458-WH (Hareline, invoice 298112) is the trigger case.
--
-- WHAT WAS ACTUALLY WRONG
--
-- Not "no normalization" — catalog_sku_match already had upc / exact / punct /
-- prefix / similar tiers. The defect is narrower and sharper:
--
--   The prefix tier prefix-matches on normalize_vendor_sku(), which RETAINS the
--   trailing truncation artifact. 'AIR2-906-...' is compared literally, dots and
--   all, so no catalogue SKU can ever start with it. The tier that exists to
--   resolve truncated SKUs is the one tier truncation defeats. Measured against
--   the live catalogue: prefix-on-normalized returns 0 candidates for every
--   truncated SKU tested; prefix-on-key returns 1-4.
--
-- Three tiers are added below. Every one is STRICTLY ADDITIVE: each fires only
-- where the current function returns nothing, and each declines on ambiguity, so
-- no input that matches today can change its answer.

-- ---------------------------------------------------------------------------
-- 1. Keys
-- ---------------------------------------------------------------------------

-- Slash-preserving match key. squeeze_vendor_sku() strips '/', which collapses
-- hook size 1/0 onto size 10 (C24411/0 vs C244110, and the same pattern on
-- Mustad C61SAP / SL53UAP, Fulling Mill 6045, Universal Salt FM8010, Ahrex 410).
-- squeeze_vendor_sku is left alone deliberately: it backs expression indexes and
-- the on_po test in po_offdoc_skus, and changing it would silently move those.
CREATE OR REPLACE FUNCTION public.sku_match_key(p_sku text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT regexp_replace(public.normalize_vendor_sku(p_sku), '[^A-Z0-9/]', '', 'g')
$$;

-- The same key with a trailing truncation artifact removed: the literal '...' a
-- narrow PDF column renders, or the single glyph fixed-width clipping leaves
-- behind ('HDN302SPR12' printing as 'HDN302SPR1:'). On a SKU that is not
-- truncated this is identical to sku_match_key, so the prefix tier can use it
-- unconditionally.
CREATE OR REPLACE FUNCTION public.sku_stem_key(p_sku text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT public.sku_match_key(
           regexp_replace(public.normalize_vendor_sku(p_sku), '[^A-Z0-9/]+$', ''))
$$;

-- Size/variant token from a document description, for disambiguating a clipped
-- SKU whose lost character is unrecoverable from the SKU alone. Hareline's line
-- reads "...Light Wire Barbless Size 12" and the Shopify variant_title is "12".
CREATE OR REPLACE FUNCTION public.sku_desc_size_token(p_desc text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT nullif(upper(btrim(
    (regexp_match(coalesce(p_desc, ''),
                  '(?:^|[^A-Za-z])SIZE[[:space:]#]*([A-Za-z0-9/]+)[[:space:]]*$',
                  'i'))[1])), '')
$$;

CREATE INDEX IF NOT EXISTS shopify_variant_index_sku_match_key_idx
  ON public.shopify_variant_index (tenant_id, public.sku_match_key(sku))
  WHERE sku IS NOT NULL;

CREATE INDEX IF NOT EXISTS shopify_variant_index_vendor_match_key_idx
  ON public.shopify_variant_index (tenant_id, vendor_id, public.sku_match_key(sku))
  WHERE sku IS NOT NULL AND vendor_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. catalog_sku_match
-- ---------------------------------------------------------------------------
-- Return type gains candidate_count / candidates, so an ambiguous result can be
-- SHOWN rather than collapsed into "no match". Dropping first: the return type
-- changes, so CREATE OR REPLACE cannot do it.
DROP FUNCTION IF EXISTS public.catalog_sku_match(uuid, uuid, text, text);

CREATE FUNCTION public.catalog_sku_match(
  p_tenant uuid, p_vendor_id uuid, p_sku text,
  p_upc text DEFAULT NULL, p_description text DEFAULT NULL)
RETURNS TABLE(matched_sku text, match_kind text, candidate_count int, candidates text[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'public'
AS $function$
  WITH norm AS (
    SELECT public.normalize_vendor_sku(p_sku)              AS s,
           nullif(public.squeeze_vendor_sku(p_sku), '')    AS z,
           nullif(public.sku_stem_key(p_sku), '')          AS stem,
           nullif(regexp_replace(coalesce(p_upc, ''), '[^0-9]', '', 'g'), '') AS u,
           public.sku_desc_size_token(p_description)       AS sz
  ),
  by_upc AS (
    SELECT v.sku, 'upc'::text AS kind
    FROM public.shopify_variant_index v, norm
    WHERE v.tenant_id = p_tenant AND norm.u IS NOT NULL
      AND btrim(v.barcode) = norm.u
    LIMIT 1
  ),
  exact AS (
    SELECT v.sku, 'exact'::text AS kind
    FROM public.shopify_variant_index v, norm
    WHERE v.tenant_id = p_tenant
      -- Redundant to the equality but not to the planner: this is what makes the
      -- partial expression index usable.
      AND v.sku IS NOT NULL
      AND public.normalize_vendor_sku(v.sku) = norm.s
    LIMIT 1
  ),

  -- punct — same characters, different punctuation. Candidate generation is
  -- UNCHANGED (legacy squeeze, tenant-wide) so nothing that matches today stops
  -- matching. The cap rises from 2 to 5 only so an ambiguous set can be narrowed
  -- and reported instead of merely counted.
  sq AS (
    SELECT DISTINCT v.sku, v.vendor_id
    FROM public.shopify_variant_index v, norm
    WHERE v.tenant_id = p_tenant AND v.sku IS NOT NULL AND norm.z IS NOT NULL
      AND public.squeeze_vendor_sku(v.sku) = norm.z
    LIMIT 5
  ),
  -- CONSTRAINT 1 — the slash, and why this is a guard rather than a key swap.
  -- Every member of sq shares one squeezed key, so more than one means they differ
  -- ONLY by punctuation; when that punctuation is a slash the pair is exactly
  -- {size 1/0, size 10}. All six such pairs in the catalogue today (C2441, C61S,
  -- FM6045, FM8010, HR410, SL53U) are SAME-vendor, so the vendor narrowing below
  -- cannot split them and they already decline on the count test. This guard is
  -- therefore defence in depth, not the thing that saves them today — it states
  -- the invariant so a later, more aggressive narrowing cannot quietly start
  -- resolving 1/0 against 10 from the printed string alone. A vendor that simply
  -- dropped the slash is indistinguishable from one that meant size 10, and
  -- guessing puts the wrong hooks in the bin.
  --
  -- Note what is NOT done here: sq's candidates are still generated with the
  -- legacy squeeze, so every input that matches today still matches. Preserving
  -- the slash in candidate GENERATION would have been the tempting move and is
  -- the unsafe one — it resolves 'C2441-1-0' to the size 10 hook.
  sq_slash AS (
    SELECT EXISTS (SELECT 1 FROM sq WHERE public.sku_match_key(sq.sku) LIKE '%/%') AS hit
  ),
  sq_vendor AS (
    SELECT sku FROM sq
    WHERE p_vendor_id IS NOT NULL AND vendor_id = p_vendor_id
      AND (SELECT count(*) FROM sq) > 1
      AND NOT (SELECT hit FROM sq_slash)
  ),
  -- CONSTRAINT 2 — vendor scoping. It removes the 16 cross-vendor collisions
  -- (718-20 vs 71820, CDC-11 vs CDC11, ...). CONSTRAINT 3 needs no denylist: the
  -- two same-vendor pairs that survive it (Renzetti SC2302-R/SC2302R, TroutHunter
  -- BGET-016/BGET016) stay ambiguous under vendor scoping too, so the count test
  -- below already declines them. A denylist table would be dead code.
  sq_one AS (
    SELECT sku, 'punct'::text AS kind FROM sq WHERE (SELECT count(*) FROM sq) = 1
    UNION ALL
    SELECT sku, 'punct'::text FROM sq_vendor
     WHERE (SELECT count(*) FROM sq_vendor) = 1
  ),

  -- Vendor's whole catalogue, keyed both ways. Guarded so the expensive scan does
  -- not run once a cheap tier has answered — without this the sweep exceeded
  -- PostgREST's statement timeout and returned no alerts at all.
  cand AS (
    SELECT v.sku,
           public.normalize_vendor_sku(v.sku) AS nsku,
           public.sku_match_key(v.sku)        AS ksku,
           v.variant_title
    FROM public.shopify_variant_index v
    WHERE v.tenant_id = p_tenant
      AND p_vendor_id IS NOT NULL AND v.vendor_id = p_vendor_id
      AND v.sku IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM by_upc)
      AND NOT EXISTS (SELECT 1 FROM exact)
      AND NOT EXISTS (SELECT 1 FROM sq_one)
  ),

  -- prefix — THE FIX. Keyed on the stem, so a truncated SKU can actually reach it.
  pfx AS (
    SELECT DISTINCT c.sku, c.variant_title
    FROM cand c, norm
    WHERE norm.stem IS NOT NULL
      AND length(norm.stem) >= 6
      AND length(c.ksku) > length(norm.stem)
      AND left(c.ksku, length(norm.stem)) = norm.stem
    LIMIT 25
  ),
  pfx_one AS (
    SELECT sku, 'prefix'::text AS kind FROM pfx WHERE (SELECT count(*) FROM pfx) = 1
  ),
  -- Clipping destroys the character rather than shifting it, so 'HDN302SPR1:'
  -- prefixes 12, 14 and 16 equally and no SKU-only rule can choose. The
  -- description can: it carries the size the SKU lost.
  pfx_desc AS (
    SELECT p.sku, 'prefix_desc'::text AS kind
    FROM pfx p, norm
    WHERE (SELECT count(*) FROM pfx) > 1
      AND norm.sz IS NOT NULL
      AND upper(btrim(coalesce(p.variant_title, ''))) = norm.sz
      AND (SELECT count(*) FROM pfx q, norm n2
            WHERE n2.sz IS NOT NULL
              AND upper(btrim(coalesce(q.variant_title, ''))) = n2.sz) = 1
  ),

  -- ozero — 0/O confusion (WPTO for WPT0, WPBO for WPB0). Vendor-scoped and
  -- ambiguity-declining, and it never fires where a stricter tier already has, so
  -- the extra keys this fold collides (22 vs 18 tenant-wide) only ever decline.
  oz AS (
    SELECT DISTINCT c.sku
    FROM cand c, norm
    WHERE norm.stem IS NOT NULL
      AND translate(c.ksku, 'O', '0') = translate(norm.stem, 'O', '0')
      AND c.ksku <> norm.stem
      AND NOT EXISTS (SELECT 1 FROM pfx_one) AND NOT EXISTS (SELECT 1 FROM pfx_desc)
    LIMIT 5
  ),
  oz_one AS (
    SELECT sku, 'ozero'::text AS kind FROM oz WHERE (SELECT count(*) FROM oz) = 1
  ),

  sim AS (
    SELECT c.sku, similarity(c.nsku, norm.s) AS s,
           row_number() OVER (ORDER BY similarity(c.nsku, norm.s) DESC, c.sku) AS rn
    FROM cand c, norm
    WHERE similarity(c.nsku, norm.s) >= 0.60
      AND NOT EXISTS (SELECT 1 FROM pfx_one) AND NOT EXISTS (SELECT 1 FROM pfx_desc)
      AND NOT EXISTS (SELECT 1 FROM oz_one)
  ),
  sim_one AS (
    SELECT b.sku, 'similar'::text AS kind
    FROM sim b LEFT JOIN sim r ON r.rn = 2
    WHERE b.rn = 1 AND (r.s IS NULL OR b.s >= r.s * 1.4)
  ),

  picked AS (
    SELECT sku, kind, 1 AS pri FROM by_upc
    UNION ALL SELECT sku, kind, 2 FROM exact
    UNION ALL SELECT sku, kind, 3 FROM sq_one
    UNION ALL SELECT sku, kind, 4 FROM pfx_one
    UNION ALL SELECT sku, kind, 5 FROM pfx_desc
    UNION ALL SELECT sku, kind, 6 FROM oz_one
    UNION ALL SELECT sku, kind, 7 FROM sim_one
  ),
  -- What to show when nothing was picked. An ambiguous set is a far more useful
  -- thing to put in front of a receiver than "no match in Shopify" — but it must
  -- NOT be dressed up as a match. It comes back as its own kind, 'ambiguous',
  -- with matched_sku NULL. Callers therefore test match_kind against the list of
  -- resolving kinds; po_offdoc_skus used to derive in_shopify from
  -- "match_kind IS NOT NULL", which would read 'ambiguous' as found-in-Shopify
  -- and reintroduce the exact wrong-instruction bug this change exists to kill.
  amb AS (
    SELECT coalesce(
      (SELECT array_agg(DISTINCT sku ORDER BY sku) FROM sq  WHERE (SELECT count(*) FROM sq)  > 1),
      (SELECT array_agg(DISTINCT sku ORDER BY sku) FROM pfx WHERE (SELECT count(*) FROM pfx) > 1),
      (SELECT array_agg(DISTINCT sku ORDER BY sku) FROM oz  WHERE (SELECT count(*) FROM oz)  > 1)
    ) AS list
  )
  -- A resolved row keeps the set the deciding tier weighed, so a prefix_desc hit
  -- records the three SKUs the description chose between. That is the audit
  -- trail: every fuzzy match is inspectable and reversible.
  SELECT (SELECT sku FROM picked ORDER BY pri LIMIT 1),
         (SELECT kind FROM picked ORDER BY pri LIMIT 1),
         greatest(coalesce(array_length((SELECT list FROM amb), 1), 0), 1),
         (SELECT list FROM amb)
  WHERE EXISTS (SELECT 1 FROM picked)
  UNION ALL
  SELECT NULL, 'ambiguous', array_length((SELECT list FROM amb), 1), (SELECT list FROM amb)
  WHERE NOT EXISTS (SELECT 1 FROM picked) AND (SELECT list FROM amb) IS NOT NULL;
$function$;
