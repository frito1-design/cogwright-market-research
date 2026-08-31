// Patch for supabase/functions/notify_off_po_skus/index.ts
//
// Apply alongside 001/002. The function's own comment is the reason this is not
// optional: "Every kind the SQL can return needs a case below ... an unlisted one
// falls to the default and tells the reader to create a product we already have."
// 001 adds three kinds — prefix_desc, ozero, ambiguous — so without this patch the
// fix makes the alert WORSE, not better.
//
// Replace the match_kind union in SweepRow, add the two new fields, and replace
// statusText wholesale. Nothing else in the file changes: buildEmailHtml and
// buildSlackText already route everything through statusText.

interface SweepRow {
  po_id: string;
  po_number: string | null;
  vendor: string | null;
  location_id: string | null;
  sku: string;
  description: string | null;
  qty: number | null;
  in_shopify: boolean;
  // Which rule in catalog_sku_match() found it. Only 'none' may be described as
  // needing creation. Saying that about a product we already stock invites a
  // duplicate variant, which then splits inventory across two SKUs.
  //
  // 'ambiguous' is NOT a match: the matcher found candidates and declined to
  // choose between them. It must never read as "add the line".
  match_kind:
    | "upc" | "exact" | "punct" | "prefix" | "prefix_desc"
    | "ozero" | "similar" | "ambiguous" | "none";
  matched_sku: string | null;
  // The catalogue SKUs the deciding tier weighed, and how many. Populated for
  // ambiguous rows and for fuzzy matches, so a receiver can see what else the
  // printed string could have meant.
  candidate_count: number | null;
  match_candidates: string[] | null;
  invoice_number: string | null;
  alert_id: string;
}

// One phrasing for both channels. The distinction that matters on the floor is
// what the receiver has to DO, so lead with that rather than with the rule's name.
function statusText(r: SweepRow): { text: string; ok: boolean } {
  const also = (n: number | null) =>
    n && n > 1 ? ` (chosen from ${n} similar SKUs — verify)` : "";

  switch (r.match_kind) {
    case "exact":
      return { text: "in Shopify — add the line", ok: true };
    case "upc":
      // The barcode agreed, whatever the two SKU strings say. Strongest evidence
      // we have, so it needs no hedging.
      return { text: `in Shopify as ${r.matched_sku} — same UPC — add the line`, ok: true };
    case "punct":
      // Identical apart from hyphens or spaces, and only ever reported when
      // exactly one catalogue SKU squeezes to that string.
      return { text: `in Shopify as ${r.matched_sku} — the vendor drops the punctuation — add the line`, ok: true };
    case "prefix":
      // The document truncated its SKU column; extended to the one catalogue SKU
      // that starts the same way.
      return { text: `in Shopify as ${r.matched_sku} — the packing list truncates it — add the line`, ok: true };
    case "prefix_desc":
      // Truncated to a stem several SKUs share; the item description carried the
      // size the SKU lost. Say so plainly — this one leans on the description, so
      // the receiver should confirm the size before adding.
      return {
        text: `in Shopify as ${r.matched_sku} — truncated SKU, matched on the size in the description${also(r.candidate_count)} — check the size, then add the line`,
        ok: true,
      };
    case "ozero":
      // 0 printed as O, or the reverse.
      return { text: `in Shopify as ${r.matched_sku} — 0/O misread in the SKU — add the line`, ok: true };
    case "similar":
      // Close but not identical, so name both strings and ask for a look. A
      // suggestion, never a resolution.
      return { text: `looks like ${r.matched_sku} in Shopify — check it matches, then add the line`, ok: true };
    case "ambiguous": {
      // The matcher found candidates and declined. This is the case that used to
      // read "no match in Shopify — check before creating it", which was wrong in
      // the most expensive direction: it is in Shopify, we just cannot say which.
      // Naming the candidates turns a bad instruction into a 10-second lookup.
      const list = (r.match_candidates ?? []).join(", ");
      return {
        text: list
          ? `could be ${list} — identify which, then add the line. Do not create a new product`
          : "matches more than one product in Shopify — identify which before creating anything",
        ok: false,
      };
    }
    default:
      return { text: "no match in Shopify — check before creating it", ok: false };
  }
}
