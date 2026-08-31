-- Carry the new match evidence through po_offdoc_skus -> po_offdoc_sku_alerts,
-- and stop deriving in_shopify from "the matcher returned something".
--
-- Apply AFTER 001_sku_match_tiers.sql.

-- Section 4.5 of the brief: every fuzzy match must be inspectable and reversible.
-- po_offdoc_sku_alerts is already the per-SKU record with a resolved_at, so it is
-- the log; it only lacked the evidence. No separate log table is introduced —
-- a second one would drift from this.
ALTER TABLE public.po_offdoc_sku_alerts
  ADD COLUMN IF NOT EXISTS candidate_count  int,
  ADD COLUMN IF NOT EXISTS match_candidates text[];

COMMENT ON COLUMN public.po_offdoc_sku_alerts.candidate_count IS
  'How many catalogue SKUs the deciding tier weighed. >1 with a matched_sku means a fuzzy tier chose between them.';
COMMENT ON COLUMN public.po_offdoc_sku_alerts.match_candidates IS
  'The candidate set considered. Populated for ambiguous rows and for fuzzy matches, so a match can be reviewed and reversed.';

-- ---------------------------------------------------------------------------
-- Both functions gain columns, so BOTH need dropping first: CREATE OR REPLACE
-- cannot change a return type, and sweep_po_offdoc_skus changes too.
DROP FUNCTION IF EXISTS public.sweep_po_offdoc_skus(uuid);
DROP FUNCTION IF EXISTS public.po_offdoc_skus(uuid);

CREATE FUNCTION public.po_offdoc_skus(p_po_id uuid)
RETURNS TABLE(sku text, description text, variant text, upc text, qty numeric,
              unit_price numeric, in_shopify boolean, match_kind text,
              matched_sku text, candidate_count int, match_candidates text[],
              email_id uuid, invoice_number text, doc_match_ratio numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'public'
AS $function$
  WITH po AS (
    SELECT id, tenant_id, vendor_id FROM public.purchase_orders WHERE id = p_po_id
  ),
  po_skus AS (
    SELECT DISTINCT public.normalize_vendor_sku(l.sku) AS sku,
                    nullif(public.squeeze_vendor_sku(l.sku), '') AS z
    FROM public.po_line_items l WHERE l.po_id = p_po_id AND l.sku IS NOT NULL
  ),
  po_upcs AS (
    SELECT DISTINCT btrim(v.barcode) AS upc
    FROM public.po_line_items l
    JOIN po ON true
    JOIN public.shopify_variant_index v
      ON v.tenant_id = po.tenant_id AND v.shopify_variant_id = l.shopify_variant_id
    WHERE l.po_id = p_po_id AND v.barcode IS NOT NULL AND btrim(v.barcode) <> ''
  ),
  docs AS (
    SELECT ve.id AS email_id, ve.invoice_number
    FROM po
    JOIN public.email_po_links epl ON epl.po_id = po.id AND epl.rejected_at IS NULL
    JOIN public.vendor_emails ve ON ve.id = epl.email_id
     AND NOT ve.is_own_po_document
     AND ve.document_type IN ('invoice','packing_slip','shipment_notification')
    -- Through the function, not by joining the view: joined, the planner cannot
    -- push email_id inside the view's aggregate.
    WHERE public.vendor_email_quantities_trustworthy(ve.id)
      AND NOT EXISTS (
        SELECT 1 FROM public.email_po_links home
        WHERE home.email_id = ve.id AND home.match_method = 'po_number_exact'
          AND home.rejected_at IS NULL AND home.po_id <> po.id)
  ),
  doc_lines AS (
    SELECT d.email_id, d.invoice_number,
           public.normalize_vendor_sku(li.sku)           AS sku,
           nullif(btrim(coalesce(li.upc, '')), '')       AS upc,
           max(li.sku)                                   AS sku_raw,
           max(li.description)                           AS description,
           max(li.variant)                               AS variant,
           sum(coalesce(li.qty_shipped, li.qty_ordered)) AS qty,
           max(li.unit_price)                            AS unit_price
    FROM docs d
    JOIN public.email_line_items li ON li.email_id = d.email_id
    WHERE li.sku IS NOT NULL
      AND coalesce(li.qty_shipped, li.qty_ordered) > 0
      AND NOT public.is_charge_line(li.sku, li.description)
    GROUP BY d.email_id, d.invoice_number,
             public.normalize_vendor_sku(li.sku),
             nullif(btrim(coalesce(li.upc, '')), '')
  ),
  -- Squeeze each document SKU once; inline in the EXISTS it is evaluated per
  -- (document line x PO line) pair instead.
  doc_keys AS (
    SELECT dl.email_id, dl.sku, dl.upc,
           nullif(public.squeeze_vendor_sku(dl.sku), '') AS z
    FROM doc_lines dl
  ),
  -- One definition of "this line is on the PO", read by both the ratio and the
  -- final filter. Three separate EXISTS rather than one OR: each is a plain
  -- equality the planner can hash-semi-join.
  doc_on_po AS (
    SELECT k.email_id, k.sku, k.upc,
           EXISTS (SELECT 1 FROM po_skus s WHERE s.sku = k.sku)
        OR (k.z IS NOT NULL AND EXISTS (SELECT 1 FROM po_skus s WHERE s.z = k.z))
        OR (k.upc IS NOT NULL AND EXISTS (SELECT 1 FROM po_upcs u WHERE u.upc = k.upc)) AS on_po
    FROM doc_keys k
  ),
  ratios AS (
    SELECT p.email_id, count(*) AS doc_skus, count(*) FILTER (WHERE p.on_po) AS matched
    FROM doc_on_po p GROUP BY p.email_id
  ),
  -- MATERIALIZED on purpose: catalog_sku_match is expensive on a miss, and the
  -- planner is otherwise free to run the lateral before this filter.
  offdoc AS MATERIALIZED (
    SELECT dl.sku_raw, dl.description, dl.variant, dl.upc, dl.qty, dl.unit_price,
           dl.email_id, dl.invoice_number,
           round(rt.matched::numeric / nullif(rt.doc_skus, 0), 3) AS doc_match_ratio
    FROM doc_lines dl
    JOIN doc_on_po p ON p.email_id = dl.email_id AND p.sku = dl.sku
     AND coalesce(p.upc, '') = coalesce(dl.upc, '')
    JOIN ratios rt ON rt.email_id = dl.email_id
    WHERE NOT p.on_po AND rt.doc_skus > 0
      AND rt.matched::numeric / rt.doc_skus >= coalesce(
            (SELECT (value #>> '{}')::numeric FROM public.system_config
              WHERE key = 'offdoc_sku.min_doc_match_ratio'), 0.50)
  )
  SELECT o.sku_raw, o.description, o.variant, o.upc, o.qty, o.unit_price,
         -- Explicit list, not "m.match_kind IS NOT NULL". 'ambiguous' means we
         -- found candidates and could not choose; calling that in_shopify would
         -- put "add the line" against a SKU nobody has identified.
         coalesce(m.match_kind, 'none')
           IN ('upc','exact','punct','prefix','prefix_desc','ozero','similar') AS in_shopify,
         coalesce(m.match_kind, 'none') AS match_kind,
         m.matched_sku, m.candidate_count, m.candidates,
         o.email_id, o.invoice_number, o.doc_match_ratio
  FROM offdoc o
  CROSS JOIN po
  LEFT JOIN LATERAL public.catalog_sku_match(
    po.tenant_id, po.vendor_id, o.sku_raw, o.upc, o.description) m ON true
  ORDER BY in_shopify DESC, o.sku_raw;
$function$;

-- ---------------------------------------------------------------------------
CREATE FUNCTION public.sweep_po_offdoc_skus(p_tenant uuid)
RETURNS TABLE(po_id uuid, po_number text, vendor text, location_id text, sku text,
              description text, qty numeric, in_shopify boolean, invoice_number text,
              alert_id uuid, variant text, upc text, match_kind text, matched_sku text,
              candidate_count int, match_candidates text[])
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'public'
AS $function$
#variable_conflict use_column
BEGIN
  IF coalesce((SELECT (value #>> '{}') FROM public.system_config
                WHERE key = 'offdoc_sku.enabled'), 'true') <> 'true' THEN
    RETURN;
  END IF;

  DROP TABLE IF EXISTS _offdoc_now;
  CREATE TEMP TABLE _offdoc_now ON COMMIT DROP AS
  SELECT po.id AS po_id, po.tenant_id, f.*
  FROM public.purchase_orders po
  CROSS JOIN LATERAL public.po_offdoc_skus(po.id) f
  WHERE po.tenant_id = p_tenant
    AND po.deleted_at IS NULL
    AND po.state IN ('sent','acknowledged','in_transit','partially_received')
    AND EXISTS (
      SELECT 1 FROM public.email_po_links epl
      JOIN public.vendor_emails ve ON ve.id = epl.email_id
      WHERE epl.po_id = po.id AND epl.rejected_at IS NULL
        AND NOT ve.is_own_po_document
        AND ve.document_type IN ('invoice','packing_slip','shipment_notification'));

  INSERT INTO public.po_offdoc_sku_alerts AS a
    (tenant_id, po_id, email_id, sku, description, qty, unit_price, in_shopify,
     match_kind, matched_sku, variant, upc, candidate_count, match_candidates)
  SELECT n.tenant_id, n.po_id, n.email_id, n.sku, n.description, n.qty, n.unit_price,
         n.in_shopify, n.match_kind, n.matched_sku, n.variant, n.upc,
         n.candidate_count, n.match_candidates
  FROM _offdoc_now n
  ON CONFLICT (tenant_id, po_id, sku, coalesce(upc, '')) DO UPDATE
    SET description = excluded.description,
        qty         = excluded.qty,
        unit_price  = excluded.unit_price,
        in_shopify  = excluded.in_shopify,
        match_kind  = excluded.match_kind,
        matched_sku = excluded.matched_sku,
        variant     = excluded.variant,
        email_id    = excluded.email_id,
        candidate_count  = excluded.candidate_count,
        match_candidates = excluded.match_candidates,
        resolved_at = NULL,
        resolved_reason = NULL
    WHERE a.resolved_at IS NOT NULL
       OR a.in_shopify  IS DISTINCT FROM excluded.in_shopify
       OR a.match_kind  IS DISTINCT FROM excluded.match_kind
       OR a.matched_sku IS DISTINCT FROM excluded.matched_sku;

  UPDATE public.po_offdoc_sku_alerts a
     SET resolved_at = now(),
         resolved_reason = 'no longer reported by the linked documents'
   WHERE a.tenant_id = p_tenant AND a.resolved_at IS NULL
     AND NOT EXISTS (
       SELECT 1 FROM _offdoc_now n
       WHERE n.po_id = a.po_id AND n.sku = a.sku
         AND coalesce(n.upc, '') = coalesce(a.upc, ''));

  RETURN QUERY
  SELECT a.po_id, po.po_number, coalesce(po.vendor_display_name, po.vendor_name_raw),
         po.location_id, a.sku, a.description, a.qty, a.in_shopify,
         n.invoice_number, a.id, a.variant, a.upc, a.match_kind, a.matched_sku,
         a.candidate_count, a.match_candidates
  FROM public.po_offdoc_sku_alerts a
  JOIN public.purchase_orders po ON po.id = a.po_id
  LEFT JOIN _offdoc_now n ON n.po_id = a.po_id AND n.sku = a.sku
   AND coalesce(n.upc, '') = coalesce(a.upc, '')
  WHERE a.tenant_id = p_tenant AND a.notified_at IS NULL AND a.resolved_at IS NULL
  ORDER BY po.po_number, a.in_shopify DESC, a.sku;
END;
$function$;
