import { api, escapeHtml, fmtDate } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Telebirr Evidence";

const STATUSES = ["available", "redeemed", "blocked", "disputed", "expired", "rejected"];

// Mirrors services/admin/queries.py's own _EVIDENCE_TRANSITIONS -- for UX
// only (which resolve buttons make sense to offer at all); the server is
// the real, sole enforcement and re-validates every one of these itself.
const TRANSITIONS = {
  available: ["blocked", "disputed"],
  blocked: ["disputed", "available"],
  disputed: ["available"],
  rejected: ["available"],
};

export async function render(container) {
  container.innerHTML = `
    <h1>Telebirr SMS evidence</h1>
    <p class="wallet-note">
      Ingested payment evidence from MacroDroid or a Telegram payment agent. Raw SMS text is loaded on
      request only, and every view of it is audited.
    </p>
    <div class="action-row">
      <label>Status
        <select id="evidence-status-filter">
          <option value="">All</option>
          ${STATUSES.map((s) => `<option value="${s}">${s}</option>`).join("")}
        </select>
      </label>
    </div>
    <div id="evidence-list"><p class="loading">Loading…</p></div>
    <div class="action-row">
      <button type="button" class="btn btn-secondary" id="evidence-load-more-btn" hidden>Load more</button>
    </div>
    <div id="evidence-detail-panel"></div>
  `;

  const listEl = container.querySelector("#evidence-list");
  const statusFilter = container.querySelector("#evidence-status-filter");
  const loadMoreBtn = container.querySelector("#evidence-load-more-btn");
  const detailPanel = container.querySelector("#evidence-detail-panel");

  let rows = [];
  let nextCursor = null;

  function statusQuery() {
    return statusFilter.value ? `&status=${encodeURIComponent(statusFilter.value)}` : "";
  }

  async function loadFirstPage() {
    listEl.innerHTML = `<p class="loading">Loading…</p>`;
    detailPanel.innerHTML = "";
    try {
      const page = await api(`/telebirr-evidence?limit=50${statusQuery()}`);
      rows = page.items;
      nextCursor = page.next_cursor;
      renderList();
    } catch (err) {
      renderError(listEl, err);
    }
  }

  async function loadMore() {
    if (nextCursor === null) return;
    try {
      const page = await api(`/telebirr-evidence?limit=50&cursor=${nextCursor}${statusQuery()}`);
      rows = rows.concat(page.items);
      nextCursor = page.next_cursor;
      renderList();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  function renderList() {
    loadMoreBtn.hidden = nextCursor === null;
    if (rows.length === 0) {
      listEl.innerHTML = `<p class="empty">No evidence matches this filter.</p>`;
      return;
    }
    listEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>ID</th><th>Source</th><th>Reference</th><th>Amount</th><th>Payer</th>
            <th>Recipient</th><th>Status</th><th>Received</th><th></th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((r) => `
            <tr data-evidence-id="${r.id}">
              <td>${r.id}</td>
              <td>${escapeHtml(r.source)}</td>
              <td>${escapeHtml(r.external_reference)}</td>
              <td>${r.amount === null ? "—" : `${r.amount} ETB`}</td>
              <td>${escapeHtml(r.payer_name || "—")}</td>
              <td>${escapeHtml(r.recipient_name || "—")}</td>
              <td><span class="badge badge-${r.status}">${escapeHtml(r.status)}</span>${r.reject_reason ? ` <span class="wallet-note">(${escapeHtml(r.reject_reason)})</span>` : ""}</td>
              <td>${fmtDate(r.received_at)}</td>
              <td>
                <button class="btn btn-secondary btn-sm view-raw-btn">View SMS</button>
                ${(TRANSITIONS[r.status] || []).map((to) => `
                  <button class="btn btn-secondary btn-sm resolve-btn" data-to="${to}">→ ${to}</button>
                `).join("")}
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
    for (const row of listEl.querySelectorAll("tr[data-evidence-id]")) {
      const evidenceId = Number(row.dataset.evidenceId);
      row.querySelector(".view-raw-btn").addEventListener("click", () => viewRawSms(evidenceId));
      for (const btn of row.querySelectorAll(".resolve-btn")) {
        btn.addEventListener("click", () => resolve(evidenceId, btn.dataset.to));
      }
    }
  }

  async function viewRawSms(evidenceId) {
    try {
      const { raw_sms } = await api(`/telebirr-evidence/${evidenceId}/raw-sms`);
      detailPanel.innerHTML = `
        <div class="detail-panel">
          <h2>Raw SMS -- evidence #${evidenceId}</h2>
          <pre class="code-block">${escapeHtml(raw_sms)}</pre>
          <button type="button" class="btn btn-secondary" id="close-raw-sms-btn">Close</button>
        </div>
      `;
      detailPanel.querySelector("#close-raw-sms-btn").addEventListener("click", () => {
        detailPanel.innerHTML = "";
      });
    } catch (err) {
      // A support/ops admin lacking payments:view_raw_evidence gets a
      // real 403 here -- surfaced as a toast, not a crash, same as every
      // other permission-gated action in this console.
      toast(err.detail || err.message, true);
    }
  }

  async function resolve(evidenceId, toStatus) {
    const reason = window.prompt(`Reason to move evidence #${evidenceId} to '${toStatus}':`);
    if (reason === null) return;
    if (!reason.trim()) {
      toast("A reason is required.", true);
      return;
    }
    try {
      await api(`/telebirr-evidence/${evidenceId}/resolve`, {
        method: "POST",
        body: { to_status: toStatus, reason },
      });
      toast(`Evidence #${evidenceId} moved to '${toStatus}'.`);
      loadFirstPage();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  statusFilter.addEventListener("change", loadFirstPage);
  loadMoreBtn.addEventListener("click", loadMore);

  await loadFirstPage();
}
