import { api, escapeHtml, fmtDate } from "../api.js";
import { toast } from "../ui.js";

export const label = "Payments";

export async function render(container) {
  container.innerHTML = `
    <h1>Withdrawals awaiting review</h1>
    <div id="withdrawals-list"><p class="loading">Loading…</p></div>
  `;
  const listEl = container.querySelector("#withdrawals-list");

  async function reload() {
    const withdrawals = await api("/withdrawals");
    renderList(withdrawals);
  }

  function renderList(withdrawals) {
    if (withdrawals.length === 0) {
      listEl.innerHTML = `<p class="empty">Nothing in review right now.</p>`;
      return;
    }
    listEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>Ref</th><th>User</th><th>Amount</th><th>Method</th><th>Destination</th>
            <th>Why in review</th><th>Requested</th><th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${withdrawals.map((w) => `
            <tr data-payment-id="${w.id}">
              <td>${escapeHtml(w.our_ref)}</td>
              <td>${escapeHtml(w.display_name)} (#${w.user_id})</td>
              <td>${w.amount} ETB</td>
              <td>${escapeHtml(w.method_kind || "—")}</td>
              <td>${escapeHtml(w.account_ref || "—")} / ${escapeHtml(w.holder_name || "—")}</td>
              <td>${escapeHtml(w.review_reason || "—")}</td>
              <td>${fmtDate(w.created_at)}</td>
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
    const reason = window.prompt(`Reason to ${action} this withdrawal:`);
    if (reason === null) return;
    if (!reason.trim()) {
      toast("A reason is required.", true);
      return;
    }
    try {
      await api(`/withdrawals/${paymentId}/${action}`, { method: "POST", body: { reason } });
      toast(action === "approve" ? "Withdrawal approved." : "Withdrawal rejected.");
      reload();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  try {
    await reload();
  } catch (err) {
    listEl.innerHTML = `<p class="error-banner">${escapeHtml(err.detail || err.message)}</p>`;
  }
}
