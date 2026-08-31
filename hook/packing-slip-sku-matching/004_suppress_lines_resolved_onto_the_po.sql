-- Suppress document lines whose resolved SKU is already on the PO.
--
-- The off-PO test (doc_on_po) compares the PRINTED SKU against the PO's SKUs. It
-- never consults the match, so a line the matcher has just identified as
-- HDN302SPR12 still counts as "not on the PO" even though the PO carries
-- HDN302SPR12 at the same quantity. That is the OTHER half of the PO-10458-WH
-- false positive: those four SKUs were wrong in the alert twice over — once as
-- "not in Shopify" and once as "not on the PO" — and 001/002 only fixed the first.
-- Brief Â§8 check 3 ("PO-10458-WH produces no alert lines for the four SKUs")
-- is not satisfied without this.
--
-- Once the printed string resolves to a catalogue SKU the PO already has, the
-- receiving grid has a line to type into and there is nothing to announce.
--
-- Apply AFTER 002. Same signature, so CREATE OR REPLACE is fine.

CREATE OR REPLACE FUNCTION public.po_offdoc_skus(p_po_id uuid)
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
  resolved AS (
    SELECT o.*, m.matched_sku, m.match_kind, m.candidate_count, m.candidates
    FROM offdoc o
    CROSS JOIN po
    LEFT JOIN LATERAL public.catalog_sku_match(
      po.tenant_id, po.vendor_id, o.sku_raw, o.upc, o.description) m ON true
  )
  SELECT r.sku_raw, r.description, r.variant, r.upc, r.qty, r.unit_price,
         -- Explicit list, not "match_kind IS NOT NULL". 'ambiguous' means we found
         -- candidates and could not choose; calling that in_shopify would put
         -- "add the line" against a SKU nobody has identified.
         coalesce(r.match_kind, 'none')
           IN ('upc','exact','punct','prefix','prefix_desc','ozero','similar') AS in_shopify,
         coalesce(r.match_kind, 'none') AS match_kind,
         r.matched_sku, r.candidate_count, r.candidates,
         r.email_id, r.invoice_number, r.doc_match_ratio
  FROM resolved r
  -- The suppression. Only a RESOLVED match counts: 'ambiguous' carries candidates
  -- but has no matched_sku, so it is never silently swallowed by this, and a
  -- genuinely new SKU (matched_sku NULL) can never be suppressed at all.
  WHERE r.matched_sku IS NULL
     OR NOT EXISTS (
          SELECT 1 FROM po_skus s
          WHERE s.sku = public.normalize_vendor_sku(r.matched_sku)
             OR (s.z IS NOT NULL
                 AND s.z = nullif(public.squeeze_vendor_sku(r.matched_sku), '')))
  ORDER BY in_shopify DESC, r.sku_raw;
$function$;
