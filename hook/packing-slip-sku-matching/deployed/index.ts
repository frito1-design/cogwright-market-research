// notify_off_po_skus — tells the warehouse when the paperwork carries a SKU the
// PO doesn't.
//
// Receiving prefills quantities from the linked invoice now, so a SKU the PO has
// never heard of is simply absent from the grid: there is no line to type into,
// and nothing on screen says the box holds more than the screen does. Asked for
// after PO-10279-WH arrived with 02-0912-10-4.3 (Jig Hot Spot Girdle -
// Gold/Brown #10) on the pack list and on neither the PO nor Shopify.
//
// This function does NO detection of its own. sweep_po_offdoc_skus() finds the
// rows, records them, retires ones since dealt with, and hands back what still
// needs announcing; all this end does is deliver and stamp notified_at. The rule
// lives in SQL precisely so the count someone is emailed and the list they see
// when they open the PO cannot drift apart — see po_offdoc_skus().
//
// The detection is gated on the document sharing the PO's SKU namespace, which
// matters more than it sounds. Ungated, the rule fires 248 times across 10 open
// POs and 222 of those come from five where ZERO SKUs matched: the vendor prints
// their own item numbers, the goods are the same, and telling the warehouse to
// "add 149 new SKUs" would be actively wrong. Gated, it is 26 rows across 5 POs.
//
// Auth (verify_jwt = false so pg_cron can call it): a valid user JWT, or
// x-sweep-token matching system_config['offdoc_sku.sweep_token'], or
// x-hook-agent-key matching HOOK_AGENT_KEY.

import { createClient, SupabaseClient } from "https://esm.sh/@supabase/supabase-js@2.49.4";
import { sendEmail, sendSlack, escapeHtml, escapeSlack } from "../_shared/notify.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-sweep-token, x-hook-agent-key",
};

const FFF_TENANT = "6308eb4f-716f-4309-8ad2-218c6c98ab0f";

const LOC_LABEL: Record<string, string> = {
  warehouse: "Warehouse",
  orem: "Orem",
  jimmys: "Jimmy's",
};

interface SweepRow {
  po_id: string;
  po_number: string | null;
  vendor: string | null;
  location_id: string | null;
  sku: string;
  description: string | null;
  qty: number | null;
  in_shopify: boolean;
  // Which rule in catalog_sku_match() found it: upc / exact / punct / prefix /
  // prefix_desc / ozero / similar / ambiguous / none. Only 'none' may be
  // described as needing creation. Saying that about a product we already stock
  // invites a duplicate variant, which then splits inventory across two SKUs —
  // Lamson PO-10349-WH said it about two reels that had been in the catalogue
  // for years. Every kind the SQL can return needs a case below for that reason:
  // an unlisted one falls to the default and tells the reader to create a
  // product we already have.
  //
  // 'ambiguous' is NOT a match: the matcher found candidates and declined to
  // choose between them. It must never read as "add the line".
  match_kind:
    | "upc" | "exact" | "punct" | "prefix" | "prefix_desc"
    | "ozero" | "similar" | "ambiguous" | "none";
  // The catalogue SKU we believe the printed one refers to. Null for 'none' and
  // for 'ambiguous'.
  matched_sku: string | null;
  // The catalogue SKUs the deciding tier weighed, and how many. Populated for
  // ambiguous rows and for fuzzy matches, so a receiver can see what else the
  // printed string could have meant.
  candidate_count: number | null;
  match_candidates: string[] | null;
  invoice_number: string | null;
  alert_id: string;
}

// One phrasing for both channels, so email and Slack cannot describe the same
// row differently. The distinction that matters on the floor is what the
// receiver has to DO, so lead with that rather than with the rule's name.
function statusText(r: SweepRow): { text: string; ok: boolean } {
  const also = (n: number | null) =>
    n && n > 1 ? ` (chosen from ${n} similar SKUs — verify)` : "";

  switch (r.match_kind) {
    case "exact":
      return { text: "in Shopify — add the line", ok: true };
    case "upc":
      // The barcode agreed, whatever the two SKU strings say. That is the
      // strongest evidence we have, so it needs no hedging.
      return { text: `in Shopify as ${r.matched_sku} — same UPC — add the line`, ok: true };
    case "punct":
      // Identical apart from hyphens or spaces, and only ever reported when
      // exactly one catalogue SKU squeezes to that string. Dr. Slick print
      // E-BOB4 as EBOB4 on every packing list.
      return { text: `in Shopify as ${r.matched_sku} — the vendor drops the punctuation — add the line`, ok: true };
    case "prefix":
      // The document truncated its SKU column; we extended it to the one
      // catalogue SKU that starts the same way.
      return { text: `in Shopify as ${r.matched_sku} — the packing list truncates it — add the line`, ok: true };
    case "prefix_desc":
      // Truncated to a stem several SKUs share; the item description carried the
      // size the SKU lost. This one leans on the description, so ask for the
      // size to be confirmed before the line is added.
      return {
        text: `in Shopify as ${r.matched_sku} — truncated SKU, matched on the size in the description${also(r.candidate_count)} — check the size, then add the line`,
        ok: true,
      };
    case "ozero":
      // 0 printed as O, or the reverse. Hareline's WPT0 prints as WPTO.
      return { text: `in Shopify as ${r.matched_sku} — 0/O misread in the SKU — add the line`, ok: true };
    case "similar":
      // Close but not identical, so name both strings and ask for a look. This
      // is a suggestion, never a resolution.
      return { text: `looks like ${r.matched_sku} in Shopify — check it matches, then add the line`, ok: true };
    case "ambiguous": {
      // The matcher found candidates and declined. This is the case that used to
      // read "no match in Shopify — check before creating it", which was wrong in
      // the most expensive direction: it IS in Shopify, we just cannot say which
      // one. Naming the candidates turns a bad instruction into a lookup.
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

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

function secretsMatch(a: string, b: string): boolean {
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// system_config values are jsonb and inconsistently shaped across features —
// some are {value: x}, some bare scalars. Accept both rather than assuming.
function unwrap(v: unknown): unknown {
  if (v && typeof v === "object" && !Array.isArray(v) && "value" in (v as Record<string, unknown>)) {
    return (v as Record<string, unknown>).value;
  }
  return v;
}

async function loadConfig(admin: SupabaseClient) {
  const { data } = await admin
    .from("system_config")
    .select("key, value")
    .in("key", [
      "offdoc_sku.enabled",
      "offdoc_sku.notify_emails",
      "offdoc_sku.sweep_token",
    ]);
  const raw = new Map((data ?? []).map((r: { key: string; value: unknown }) => [r.key, unwrap(r.value)]));
  const emails = raw.get("offdoc_sku.notify_emails");
  return {
    enabled: String(raw.get("offdoc_sku.enabled") ?? "true") !== "false",
    notifyEmails: (Array.isArray(emails) ? emails : [])
      .map((s) => String(s).trim()).filter(Boolean),
    sweepToken: String(raw.get("offdoc_sku.sweep_token") ?? ""),
  };
}

// The tenant's Slack Incoming Webhook, from app_settings 'notify_slack_webhook'
// = { url, enabled }. The SAME source dispatch_nudges and purchasing_queue_health
// read — one webhook setting, not a third one to keep in sync. `enabled: false`
// or a URL that isn't a Slack hook means no Slack, which is a configuration
// state and not a failure.
async function slackWebhookFor(admin: SupabaseClient, tenantId: string): Promise<string> {
  const { data } = await admin
    .from("app_settings").select("value")
    .eq("key", "notify_slack_webhook").eq("tenant_id", tenantId).maybeSingle();
  const val = (data?.value ?? {}) as { url?: unknown; enabled?: unknown };
  const url = typeof val.url === "string" ? val.url.trim() : "";
  return val.enabled === true && url.startsWith("https://hooks.slack.com/") ? url : "";
}

// Quantities are printed AS THE VENDOR PRINTED THEM, and the wording says so.
// A SKU that is on neither the PO nor Shopify has no pack size anywhere to
// convert by — MFC's "2" for 02-0912-10-4.3 is two dozen at $22.95 a dozen, but
// nothing in our data knows that yet. Asserting eaches would be a guess; the
// vendor's own figure is a fact.
function qtyText(qty: number | null): string {
  if (qty == null) return "—";
  const n = Number(qty);
  if (!Number.isFinite(n)) return "—";
  // numeric comes back as "2.00"; whole numbers read as whole numbers.
  return Number.isInteger(n) ? String(n) : String(Number(n.toFixed(2)));
}

function buildEmailHtml(byPo: Map<string, SweepRow[]>): string {
  const blocks: string[] = [];
  for (const rows of byPo.values()) {
    const h = rows[0];
    const loc = LOC_LABEL[h.location_id ?? ""] ?? h.location_id ?? "";
    const lines = rows.map((r) => {
      const st = statusText(r);
      const tag = `<span style="color:${st.ok ? "#1a7f37" : "#b42318"};font-weight:600">${escapeHtml(st.text)}</span>`;
      return `<tr>
        <td style="padding:4px 10px 4px 0;font-family:monospace">${escapeHtml(r.sku)}</td>
        <td style="padding:4px 10px 4px 0">${escapeHtml(r.description ?? "—")}</td>
        <td style="padding:4px 10px 4px 0;text-align:right">${escapeHtml(qtyText(r.qty))}</td>
        <td style="padding:4px 0">${tag}</td>
      </tr>`;
    }).join("");
    blocks.push(`
      <p style="margin:18px 0 6px"><strong>${escapeHtml(h.po_number ?? "(no number)")}</strong>
        · ${escapeHtml(h.vendor ?? "")}${loc ? ` · ${escapeHtml(loc)}` : ""}
        ${h.invoice_number ? ` · invoice ${escapeHtml(h.invoice_number)}` : ""}</p>
      <table style="border-collapse:collapse;font-size:13px">
        <tr style="text-align:left;color:#667085;font-size:11px;text-transform:uppercase">
          <th style="padding:0 10px 4px 0">SKU</th><th style="padding:0 10px 4px 0">Item</th>
          <th style="padding:0 10px 4px 0;text-align:right">Qty</th><th style="padding:0 0 4px">Status</th>
        </tr>
        ${lines}
      </table>`);
  }

  return `<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;font-size:14px;color:#1d2939">
    <p>The shipping paperwork for ${byPo.size === 1 ? "a purchase order" : `${byPo.size} purchase orders`}
       lists ${[...byPo.values()].flat().length === 1 ? "an item" : "items"} the PO doesn't have.</p>
    ${blocks.join("")}
    <p style="margin-top:18px;color:#667085;font-size:12px">
      Quantities are as the vendor printed them — an item that is on neither the PO nor Shopify has no
      pack size recorded anywhere, so these are not converted to each.<br>
      Open the PO's receive screen to add the lines. Items already in Shopify can be added there directly;
      the rest need the product created first.
    </p>
  </div>`;
}

function buildSlackText(byPo: Map<string, SweepRow[]>): string {
  const parts: string[] = [];
  const total = [...byPo.values()].flat().length;
  parts.push(`*${total} item${total === 1 ? "" : "s"} on shipping paperwork ${total === 1 ? "isn't" : "aren't"} on the PO*`);
  for (const rows of byPo.values()) {
    const h = rows[0];
    const loc = LOC_LABEL[h.location_id ?? ""] ?? h.location_id ?? "";
    parts.push(`\n*${escapeSlack(h.po_number ?? "(no number)")}* · ${escapeSlack(h.vendor ?? "")}${loc ? ` · ${escapeSlack(loc)}` : ""}`);
    for (const r of rows) {
      const st = statusText(r);
      parts.push(`• \`${escapeSlack(r.sku)}\`  ${escapeSlack(r.description ?? "—")}  · qty ${escapeSlack(qtyText(r.qty))}  · ${st.ok ? escapeSlack(st.text) : `*${escapeSlack(st.text)}*`}`);
    }
  }
  parts.push(`\n_Quantities are as the vendor printed them._`);
  return parts.join("\n");
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  try {
    const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
    const SERVICE_ROLE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
    const ANON = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
    if (!SUPABASE_URL || !SERVICE_ROLE) return json({ error: "not configured" }, 500);

    const admin = createClient(SUPABASE_URL, SERVICE_ROLE);
    const body = (await req.json().catch(() => ({}))) as Record<string, unknown>;
    const cfg = await loadConfig(admin);

    // --- auth -------------------------------------------------------------
    const presentedSweep = req.headers.get("x-sweep-token") ?? "";
    const presentedAgent = req.headers.get("x-hook-agent-key") ?? "";
    const agentKey = Deno.env.get("HOOK_AGENT_KEY") ?? "";
    let caller = "";
    let callerTenant: string | null = null;
    if (cfg.sweepToken && secretsMatch(presentedSweep, cfg.sweepToken)) {
      caller = "sweep_token";
    } else if (agentKey && secretsMatch(presentedAgent, agentKey)) {
      caller = "agent_key";
    } else {
      const authHeader = req.headers.get("Authorization") ?? "";
      if (authHeader.startsWith("Bearer ")) {
        const userClient = createClient(SUPABASE_URL, ANON, {
          global: { headers: { Authorization: authHeader } },
        });
        const { data: claims } = await userClient.auth.getClaims(authHeader.replace("Bearer ", ""));
        if (claims?.claims?.sub) {
          caller = `user:${claims.claims.sub}`;
          // A signed-in caller always sweeps their OWN tenant. Taking the tenant
          // from the body would let any logged-in user — a demo account
          // included — run this across Fly Fish Food's purchase orders and fire
          // its notifications, the hole that had to be closed in
          // check_po_cost_drift.
          const { data: prof } = await admin
            .from("profiles").select("tenant_id").eq("id", claims.claims.sub).maybeSingle();
          callerTenant = (prof?.tenant_id as string | undefined) ?? null;
        }
      }
    }
    if (!caller) return json({ error: "Unauthorized" }, 401);

    const tenantId = callerTenant ?? String(body.tenant_id ?? FFF_TENANT);
    // dry_run reports what WOULD be sent and leaves notified_at alone, so the
    // sweep can be inspected on real data without spending anyone's attention.
    const dryRun = body.dry_run === true;

    if (!cfg.enabled) return json({ ok: true, skipped: "offdoc_sku.enabled=false" });

    // --- detect (all of it, in SQL) ---------------------------------------
    const { data: rows, error: sweepErr } = await admin
      .rpc("sweep_po_offdoc_skus", { p_tenant: tenantId });
    if (sweepErr) return json({ error: `sweep failed: ${sweepErr.message}` }, 500);

    const pending = (rows ?? []) as SweepRow[];
    if (pending.length === 0) {
      return json({ ok: true, tenant_id: tenantId, pending: 0, notified: false });
    }

    const byPo = new Map<string, SweepRow[]>();
    for (const r of pending) {
      const list = byPo.get(r.po_id);
      if (list) list.push(r); else byPo.set(r.po_id, [r]);
    }

    const total = pending.length;
    const notInShopify = pending.filter((r) => !r.in_shopify).length;
    const subject = total === 1
      ? `1 item on a packing list isn't on the PO`
      : `${total} items on packing lists aren't on their POs`;

    if (dryRun) {
      return json({
        ok: true, dry_run: true, tenant_id: tenantId,
        pending: total, pos: byPo.size, not_in_shopify: notInShopify, subject,
        rows: pending.map((r) => ({
          po: r.po_number, sku: r.sku, qty: r.qty, in_shopify: r.in_shopify,
          match_kind: r.match_kind, matched_sku: r.matched_sku,
          candidate_count: r.candidate_count, match_candidates: r.match_candidates,
        })),
      });
    }

    // --- deliver ----------------------------------------------------------
    // Each channel is independent. A disabled or absent Slack webhook is a
    // configuration state, not a failure, and must not stop the email.
    let emailStatus: string | null = null;
    let emailError: string | null = null;
    if (cfg.notifyEmails.length > 0) {
      const res = await sendEmail({
        to: cfg.notifyEmails,
        subject: `${subject} — Hook`,
        html: buildEmailHtml(byPo),
      });
      emailStatus = res.ok ? "sent" : "failed";
      emailError = res.ok ? null : res.error ?? "unknown";
    }

    let slackStatus: string | null = null;
    let slackError: string | null = null;
    const slackWebhook = await slackWebhookFor(admin, tenantId);
    if (slackWebhook) {
      const res = await sendSlack({ webhookUrl: slackWebhook, text: buildSlackText(byPo) });
      slackStatus = res.ok ? "sent" : "failed";
      slackError = res.ok ? null : res.error ?? "unknown";
    }

    // Stamp only if something actually went out. If every configured channel
    // failed the rows stay pending and the next sweep tries again — silently
    // marking them notified would lose the alert entirely.
    const delivered = emailStatus === "sent" || slackStatus === "sent";
    let stampError: string | null = null;
    if (delivered) {
      const { error } = await admin
        .from("po_offdoc_sku_alerts")
        .update({ notified_at: new Date().toISOString() })
        .in("id", pending.map((r) => r.alert_id));
      if (error) stampError = error.message;
    }

    const payload = {
      pos: [...byPo.values()].map((rows) => ({
        po_number: rows[0].po_number,
        vendor: rows[0].vendor,
        location_id: rows[0].location_id,
        skus: rows.map((r) => ({ sku: r.sku, qty: r.qty, in_shopify: r.in_shopify })),
      })),
      total, not_in_shopify: notInShopify, caller,
    };

    for (const [channel, status, error, recipient] of [
      ["email", emailStatus, emailError, cfg.notifyEmails.join(", ") || null],
      ["slack", slackStatus, slackError, null],
    ] as const) {
      if (status == null) continue;
      await admin.from("notifications").insert({
        tenant_id: tenantId,
        category: "purchasing",
        event_key: "po_offdoc_sku",
        channel,
        recipient_email: recipient,
        subject,
        status,
        error,
        payload,
      });
    }

    return json({
      ok: true, tenant_id: tenantId, pending: total, pos: byPo.size,
      not_in_shopify: notInShopify,
      email: emailStatus, email_error: emailError,
      slack: slackStatus ?? "not configured", slack_error: slackError,
      notified: delivered, stamp_error: stampError,
    });
  } catch (err) {
    return json({ error: (err as Error).message || String(err) }, 500);
  }
});
