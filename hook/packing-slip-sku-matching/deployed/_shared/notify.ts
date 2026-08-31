// Shared notification dispatch for the Hook app.
//
// One place that knows how to deliver a notification, so the growing list of
// triggers (change-request status, workflow/approval nudges, …) stops
// re-implementing Resend boilerplate. Email is the only channel today; the
// signature is deliberately channel-agnostic so a Slack webhook (or n8n route)
// drops in later without touching call sites.
//
// Usage:
//   import { sendEmail, escapeHtml, DEFAULT_FROM } from "../_shared/notify.ts";
//   const r = await sendEmail({ to, subject, html });
//   if (!r.ok) console.error("notify failed", r.error);

import { Resend } from "npm:resend@2.0.0";

// Internal-facing "from" for app notifications. Vendor-facing PO sends resolve
// their own per-location "from" via app_settings; this is for messages to our
// own team, matching notify_change_request_status.
export const DEFAULT_FROM = "Fly Fish Food App <apinv@flyfishfood.com>";

export type NotifyChannel = "email" | "slack";

export interface EmailMessage {
  to: string | string[];
  subject: string;
  html: string;
  cc?: string | string[];
  bcc?: string | string[];
  from?: string;
  replyTo?: string;
}

export interface NotifyResult {
  ok: boolean;
  channel: NotifyChannel;
  id?: string;
  error?: string;
}

export function escapeHtml(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function asArray(v: string | string[] | undefined): string[] | undefined {
  if (v == null) return undefined;
  const arr = (Array.isArray(v) ? v : [v]).map((s) => s.trim()).filter(Boolean);
  return arr.length ? arr : undefined;
}

// Send one email via Resend. Never throws — returns a result the caller can log
// so a delivery failure never takes down the surrounding job.
export async function sendEmail(msg: EmailMessage): Promise<NotifyResult> {
  const to = asArray(msg.to);
  if (!to || to.length === 0) {
    return { ok: false, channel: "email", error: "no recipient" };
  }

  const resendKey = Deno.env.get("RESEND_API_KEY");
  if (!resendKey) {
    return { ok: false, channel: "email", error: "RESEND_API_KEY not configured" };
  }

  try {
    const resend = new Resend(resendKey);
    const { data, error } = await resend.emails.send({
      from: msg.from || DEFAULT_FROM,
      to,
      cc: asArray(msg.cc),
      bcc: asArray(msg.bcc),
      replyTo: msg.replyTo,
      subject: msg.subject,
      html: msg.html,
    });
    if (error) {
      return { ok: false, channel: "email", error: (error as { message?: string }).message ?? String(error) };
    }
    return { ok: true, channel: "email", id: (data as { id?: string } | null)?.id };
  } catch (err) {
    return { ok: false, channel: "email", error: (err as Error).message || String(err) };
  }
}

export interface SlackMessage {
  // A Slack Incoming Webhook URL (https://hooks.slack.com/services/...). The
  // destination channel is fixed by the webhook, so we only send content.
  webhookUrl: string;
  // Fallback / notification text. Also used as the message body when no blocks
  // are given. Slack mrkdwn is supported.
  text: string;
  // Optional Block Kit blocks for richer layout; `text` remains the fallback.
  blocks?: unknown[];
}

// Post a message to a Slack Incoming Webhook. A plain webhook post needs no SDK
// and no n8n — just an HTTPS POST. Never throws; returns a result to log.
export async function sendSlack(msg: SlackMessage): Promise<NotifyResult> {
  const url = (msg.webhookUrl ?? "").trim();
  if (!url.startsWith("https://hooks.slack.com/")) {
    return { ok: false, channel: "slack", error: "invalid or missing Slack webhook URL" };
  }
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(msg.blocks ? { text: msg.text, blocks: msg.blocks } : { text: msg.text }),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      return { ok: false, channel: "slack", error: `slack ${res.status}: ${detail.slice(0, 200)}` };
    }
    return { ok: true, channel: "slack" };
  } catch (err) {
    return { ok: false, channel: "slack", error: (err as Error).message || String(err) };
  }
}

// Slack mrkdwn escaping — only &, <, > are special in Slack message text.
export function escapeSlack(s: unknown): string {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Thin wrapper that fans out to a channel. Call sites target notify() and pass
// whichever payload matches the channel; adding a channel is a new case here.
export interface NotifyRequest {
  channel: NotifyChannel;
  email?: EmailMessage;
  slack?: SlackMessage;
}

export async function notify(req: NotifyRequest): Promise<NotifyResult> {
  switch (req.channel) {
    case "email":
      if (!req.email) return { ok: false, channel: "email", error: "no email payload" };
      return sendEmail(req.email);
    case "slack":
      if (!req.slack) return { ok: false, channel: "slack", error: "no slack payload" };
      return sendSlack(req.slack);
    default:
      return { ok: false, channel: "email", error: `unsupported channel: ${req.channel}` };
  }
}
