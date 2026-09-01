import { api, escapeHtml, fmtDate } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Manual Deposits";

export async function render(container) {
  container.innerHTML = `
    <h1>Manual deposits awaiting review</h1>
    <div id="manual-deposits-list"><p class="loading">Loading…</p></div>
  `;
  const listEl = container.querySelector("#manual-deposits-list");

  async function reload() {
    const deposits = await api("/manual-deposits");
    renderList(deposits);
  }

  function renderList(deposits) {
    if (deposits.length === 0) {
      listEl.innerHTML = `<p class="empty">Nothing in review right now.</p>`;
      return;
    }
    listEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>Ref</th><th>User</th><th>Amount</th><th>Method</th><th>Destination</th>
            <th>Player reference</th><th>Receipt</th><th>Requested</th><th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${deposits.map((d) => `
            <tr data-payment-id="${d.id}">
              <td>${escapeHtml(d.our_ref)}</td>
              <td>${escapeHtml(d.display_name)} (#${d.user_id})</td>
              <td>${d.amount} ETB</td>
              <td>${escapeHtml(d.method_kind || "—")}</td>
              <td>${escapeHtml(d.destination_account_name || "—")} (${escapeHtml(d.destination_account_ref || "—")})</td>
              <td>
                ${escapeHtml(d.external_reference || "—")}
                ${d.possible_duplicate_reference ? '<span class="badge badge-review">possible duplicate</span>' : ""}
                ${d.first_approved_by_admin_id ? `<span class="badge badge-review">awaiting 2nd approval (1st: ${escapeHtml(d.first_approver_username)})</span>` : ""}
              </td>
              <td>${d.receipt_telegram_file_id ? `<a href="/manual-deposits/${d.id}/receipt" target="_blank" rel="noopener">view</a>` : "—"}</td>
              <td>${fmtDate(d.created_at)}</td>
              <td>
                <button class="btn btn-success btn-sm approve-btn">Approve</button>
                <button class="btn btn-danger btn-sm reject-btn">Reject</button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
    for (const row of listEl.querySelectorAll("tr[data-payment-id]")) {
      const paymentId = Number(row.dataset.paymentId);
      row.querySelector(".approve-btn").addEventListener("click", () => decide(paymentId, "approve"));
      row.querySelector(".reject-btn").addEventListener("click", () => decide(paymentId, "reject"));
    }
  }

  async function decide(paymentId, action) {
    const reason = window.prompt(`Reason to ${action} this deposit:`);
    if (reason === null) return;
    if (!reason.trim()) {
      toast("A reason is required.", true);
      return;
    }
    try {
      const result = await api(`/manual-deposits/${paymentId}/${action}`, { method: "POST", body: { reason } });
      if (action === "reject") {
        toast("Deposit rejected.");
      } else if (result.outcome === "awaiting_second_approval") {
        toast("First approval recorded -- a different admin must approve to credit this deposit.");
      } else {
        toast("Deposit approved and credited.");
      }
      reload();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  try {
    await reload();
  } catch (err) {
    renderError(listEl, err);
  }
}
