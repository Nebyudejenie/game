import { api, escapeHtml, fmtDate } from "../api.js";
import { renderError } from "../ui.js";

export const label = "Telegram";

const STATUS_EXPLAINER = {
  healthy: "Telegram is delivering updates normally.",
  warning: "A backlog is building up -- worth a look, not yet an incident.",
  critical: "A large backlog is stuck -- Telegram cannot reach the webhook, or it is failing to process updates.",
};

export async function render(container) {
  container.innerHTML = `
    <h1>Telegram</h1>
    <p class="empty">
      A live check against Telegram's own getWebhookInfo -- made fresh every
      time this page loads, not cached. Per-command latency (P50/P95/P99) and
      DB/Redis/Telegram-API time breakdown live on the Grafana dashboard,
      which can compute real percentiles over time; this page answers the
      simpler, more urgent question -- "is Telegram reaching us right now?"
    </p>
    <div id="webhook-health"><p class="loading">Loading…</p></div>
  `;

  const el = container.querySelector("#webhook-health");
  try {
    const h = await api("/telegram/webhook-health");
    el.innerHTML = `
      <div class="stat-grid">
        <div class="stat-card${h.status !== "healthy" ? " stat-card-alert" : ""}">
          <div class="stat-label">Status</div>
          <div class="stat-value"><span class="badge badge-${h.status}">${h.status}</span></div>
        </div>
        <div class="stat-card${h.pending_update_count >= h.warning_threshold ? " stat-card-alert" : ""}">
          <div class="stat-label">Pending updates</div>
          <div class="stat-value">${h.pending_update_count}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Webhook URL</div>
          <div class="stat-value" style="font-size:0.85rem;word-break:break-all">${escapeHtml(h.url || "(not set)")}</div>
        </div>
      </div>

      <div class="detail-panel">
        <p>${STATUS_EXPLAINER[h.status] || ""}</p>
        <div class="detail-grid">
          <div><div class="field-label">Last error</div><div class="field-value">${h.last_error_message ? escapeHtml(h.last_error_message) : "None"}</div></div>
          <div><div class="field-label">Last error at</div><div class="field-value">${h.last_error_date ? fmtDate(h.last_error_date) : "—"}</div></div>
          <div><div class="field-label">Last sync error at</div><div class="field-value">${h.last_synchronization_error_date ? fmtDate(h.last_synchronization_error_date) : "—"}</div></div>
          <div><div class="field-label">IP address</div><div class="field-value">${h.ip_address ? escapeHtml(h.ip_address) : "—"}</div></div>
          <div><div class="field-label">Max connections</div><div class="field-value">${h.max_connections ?? "—"}</div></div>
        </div>
        <p class="empty" style="margin-top:0.75rem">
          Telegram's own API does not report how long the oldest pending
          update has been waiting -- only a raw count. Pending updates
          climbing over time, alongside a falling
          <code>telegram_commands_total</code> rate on Grafana, is the real
          signal of a stuck backlog rather than a brief, self-clearing spike.
        </p>
      </div>
    `;
  } catch (err) {
    renderError(el, err);
  }
}
