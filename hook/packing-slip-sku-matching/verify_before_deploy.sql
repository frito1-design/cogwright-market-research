-- Pre-deploy verification for 001_sku_match_tiers.sql.
--
-- READ-ONLY and SELF-CONTAINED: the new keys are inlined as expressions, so this
-- runs against production BEFORE the migration and proves the proposed tiers on
-- live data. Run it again after deploying; every row must still say PASS.
--
--   K(x)    = regexp_replace(normalize_vendor_sku(x), '[^A-Z0-9/]', '', 'g')
--   STEM(x) = K(regexp_replace(normalize_vendor_sku(x), '[^A-Z0-9/]+$', ''))

WITH tests(label, po_number, sku, descr, expect_kind, expect_sku) AS (VALUES
  -- Section 2 of the brief: the four SKUs that produced the false alert.
  ('trigger clip 12', 'PO-10458-WH', 'HDN302SPR1:', 'Dohiku HDN302SPR Jig Nymph Hook Light Wire Barbless Size 12', 'prefix_desc', 'HDN302SPR12'),
  ('trigger clip 14', 'PO-10458-WH', 'HDN302SPR1.', 'Dohiku HDN302SPR Jig Nymph Hook Light Wire Barbless Size 14', 'prefix_desc', 'HDN302SPR14'),
  ('trigger clip 16', 'PO-10458-WH', 'HDN302SPR1(', 'Dohiku HDN302SPR Jig Nymph Hook Light Wire Barbless Size 16', 'prefix_desc', 'HDN302SPR16'),
  ('trigger 0/O',     'PO-10458-WH', 'WPTO',        'Plated Lead Eyes Micro #0',                                   'ozero',       'WPT0'),
  -- Clean truncation, no trailing glyph: the description tiebreak must reach it too.
  ('clean truncation','PO-10460-JM', 'HDN302SPR1',  'Dohiku HDN302SPR Jig Nymph Hook Light Wire Barbless Size 16', 'prefix_desc', 'HDN302SPR16'),
  -- REGRESSION. The generic punct rule already absorbs Dr. Slick; it must survive.
  ('dr slick punct',  'PO-10408-WH', 'EBOB4',       'Barb Bender',                                                 'punct',       'E-BOB4'),
  ('dr slick punct2', 'PO-10408-WH', 'COMBO5G',     'Combo Kit',                                                   'punct',       'COMBO-5G'),
  -- Ambiguity must DECLINE, not guess: same stem, description carries no size.
  ('ambiguous stem',  'PO-10458-WH', 'HDN302SPR1:', 'Dohiku Jig Nymph Hook',                                       'ambiguous',   NULL),
  -- CONSTRAINT 1. 'C2441-1/0' has no exact match and squeezes onto BOTH the 1/0
  -- hook and the size 10 hook. It must decline and name them, never pick one.
  ('slash 1/0 vs 10', 'PO-10458-WH', 'C2441-1/0',   'Core C2441 Steelhead Salmon Hook',                            'ambiguous',   NULL)
),
t AS (
  SELECT x.*, po.tenant_id, po.vendor_id
  FROM tests x JOIN public.purchase_orders po ON po.po_number = x.po_number
),
r AS (
  SELECT t.*, m.matched_sku, m.match_kind, m.candidate_count, m.candidates
  FROM t, LATERAL (
    WITH norm AS (
      SELECT public.normalize_vendor_sku(t.sku) AS s,
             nullif(public.squeeze_vendor_sku(t.sku), '') AS z,
             nullif(regexp_replace(public.normalize_vendor_sku(
               regexp_replace(public.normalize_vendor_sku(t.sku), '[^A-Z0-9/]+$', '')),
               '[^A-Z0-9/]', '', 'g'), '') AS stem,
             nullif(upper(btrim((regexp_match(coalesce(t.descr, ''),
               '(?:^|[^A-Za-z])SIZE[[:space:]#]*([A-Za-z0-9/]+)[[:space:]]*$', 'i'))[1])), '') AS sz
    ),
    exact AS (
      SELECT v.sku, 'exact'::text AS kind FROM public.shopify_variant_index v, norm
      WHERE v.tenant_id = t.tenant_id AND v.sku IS NOT NULL
        AND public.normalize_vendor_sku(v.sku) = norm.s LIMIT 1
    ),
    sq AS (
      SELECT DISTINCT v.sku, v.vendor_id FROM public.shopify_variant_index v, norm
      WHERE v.tenant_id = t.tenant_id AND v.sku IS NOT NULL AND norm.z IS NOT NULL
        AND public.squeeze_vendor_sku(v.sku) = norm.z LIMIT 5
    ),
    sq_slash AS (
      SELECT EXISTS (SELECT 1 FROM sq
        WHERE regexp_replace(public.normalize_vendor_sku(sq.sku), '[^A-Z0-9/]', '', 'g') LIKE '%/%') AS hit
    ),
    sq_vendor AS (
      SELECT sku FROM sq
      WHERE t.vendor_id IS NOT NULL AND vendor_id = t.vendor_id
        AND (SELECT count(*) FROM sq) > 1 AND NOT (SELECT hit FROM sq_slash)
    ),
    sq_one AS (
      SELECT sku, 'punct'::text AS kind FROM sq WHERE (SELECT count(*) FROM sq) = 1
      UNION ALL
      SELECT sku, 'punct'::text FROM sq_vendor WHERE (SELECT count(*) FROM sq_vendor) = 1
    ),
    cand AS (
      SELECT v.sku, public.normalize_vendor_sku(v.sku) AS nsku,
             regexp_replace(public.normalize_vendor_sku(v.sku), '[^A-Z0-9/]', '', 'g') AS ksku,
             v.variant_title
      FROM public.shopify_variant_index v
      WHERE v.tenant_id = t.tenant_id AND t.vendor_id IS NOT NULL
        AND v.vendor_id = t.vendor_id AND v.sku IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM exact) AND NOT EXISTS (SELECT 1 FROM sq_one)
    ),
    pfx AS (
      SELECT DISTINCT c.sku, c.variant_title FROM cand c, norm
      WHERE norm.stem IS NOT NULL AND length(norm.stem) >= 6
        AND length(c.ksku) > length(norm.stem)
        AND left(c.ksku, length(norm.stem)) = norm.stem
      LIMIT 25
    ),
    pfx_one AS (SELECT sku, 'prefix'::text AS kind FROM pfx WHERE (SELECT count(*) FROM pfx) = 1),
    pfx_desc AS (
      SELECT p.sku, 'prefix_desc'::text AS kind FROM pfx p, norm
      WHERE (SELECT count(*) FROM pfx) > 1 AND norm.sz IS NOT NULL
        AND upper(btrim(coalesce(p.variant_title, ''))) = norm.sz
        AND (SELECT count(*) FROM pfx q, norm n2 WHERE n2.sz IS NOT NULL
              AND upper(btrim(coalesce(q.variant_title, ''))) = n2.sz) = 1
    ),
    oz AS (
      SELECT DISTINCT c.sku FROM cand c, norm
      WHERE norm.stem IS NOT NULL
        AND translate(c.ksku, 'O', '0') = translate(norm.stem, 'O', '0')
        AND c.ksku <> norm.stem
        AND NOT EXISTS (SELECT 1 FROM pfx_one) AND NOT EXISTS (SELECT 1 FROM pfx_desc)
      LIMIT 5
    ),
    oz_one AS (SELECT sku, 'ozero'::text AS kind FROM oz WHERE (SELECT count(*) FROM oz) = 1),
    picked AS (
      SELECT sku, kind, 2 AS pri FROM exact
      UNION ALL SELECT sku, kind, 3 FROM sq_one
      UNION ALL SELECT sku, kind, 4 FROM pfx_one
      UNION ALL SELECT sku, kind, 5 FROM pfx_desc
      UNION ALL SELECT sku, kind, 6 FROM oz_one
    ),
    amb AS (
      SELECT coalesce(
        (SELECT array_agg(DISTINCT sku ORDER BY sku) FROM sq  WHERE (SELECT count(*) FROM sq)  > 1),
        (SELECT array_agg(DISTINCT sku ORDER BY sku) FROM pfx WHERE (SELECT count(*) FROM pfx) > 1),
        (SELECT array_agg(DISTINCT sku ORDER BY sku) FROM oz  WHERE (SELECT count(*) FROM oz)  > 1)
      ) AS list
    )
    SELECT (SELECT sku FROM picked ORDER BY pri LIMIT 1) AS matched_sku,
           coalesce((SELECT kind FROM picked ORDER BY pri LIMIT 1),
                    CASE WHEN (SELECT list FROM amb) IS NOT NULL
                         THEN 'ambiguous' ELSE 'none' END) AS match_kind,
           greatest(coalesce(array_length((SELECT list FROM amb), 1), 0), 1) AS candidate_count,
           (SELECT list FROM amb) AS candidates
  ) m
)
SELECT label, sku, expect_kind, match_kind, expect_sku, matched_sku,
       candidate_count, candidates,
       CASE WHEN match_kind = expect_kind
             AND coalesce(matched_sku, '~') = coalesce(expect_sku, '~')
            THEN 'PASS' ELSE 'FAIL' END AS result
FROM r ORDER BY label;
