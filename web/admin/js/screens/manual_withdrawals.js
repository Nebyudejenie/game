import { api, escapeHtml, fmtDate } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Manual Withdrawals";

export async function render(container) {
  container.innerHTML = `
    <h1>Manual withdrawals awaiting review</h1>
    <div id="manual-withdrawals-pending"><p class="loading">Loading…</p></div>

    <h2>Approved -- awaiting settlement</h2>
    <p class="wallet-note">
      Send the transfer externally first, then settle here with the real reference number.
    </p>
    <div id="manual-withdrawals-settlement"><p class="loading">Loading…</p></div>
  `;
  const pendingEl = container.querySelector("#manual-withdrawals-pending");
  const settlementEl = container.querySelector("#manual-withdrawals-settlement");

  async function reloadPending() {
    const withdrawals = await api("/manual-withdrawals");
    renderPending(withdrawals);
  }

  async function reloadSettlement() {
    const withdrawals = await api("/manual-withdrawals/awaiting-settlement");
    renderSettlement(withdrawals);
  }

  function renderPending(withdrawals) {
    if (withdrawals.length === 0) {
      pendingEl.innerHTML = `<p class="empty">Nothing in review right now.</p>`;
      return;
    }
    pendingEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>Ref</th><th>User</th><th>KYC</th><th>Amount</th><th>Destination</th>
            <th>Prior payouts</th><th>Why in review</th><th>Requested</th><th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${withdrawals.map((w) => `
            <tr data-payment-id="${w.id}">
              <td>${escapeHtml(w.our_ref)}</td>
              <td>${escapeHtml(w.display_name)} (#${w.user_id})</td>
              <td>${w.kyc_level}</td>
              <td>${w.amount} ETB</td>
              <td>${escapeHtml(w.method_kind || "—")} / ${escapeHtml(w.account_ref || "—")} / ${escapeHtml(w.holder_name || "—")}</td>
              <td>${w.prior_successful_withdrawals}</td>
              <td>
                ${escapeHtml(w.review_reason || "—")}
                ${w.first_approved_by_admin_id ? `<span class="badge badge-review">awaiting 2nd approval (1st: ${escapeHtml(w.first_approver_username)})</span>` : ""}
              </td>
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
    for (const row of pendingEl.querySelectorAll("tr[data-payment-id]")) {
      const paymentId = Number(row.dataset.paymentId);
      row.querySelector(".approve-btn").addEventListener("click", () => approve(paymentId));
      row.querySelector(".reject-btn").addEventListener("click", () => reject(paymentId));
    }
  }

  function renderSettlement(withdrawals) {
    if (withdrawals.length === 0) {
      settlementEl.innerHTML = `<p class="empty">Nothing waiting on a transfer right now.</p>`;
      return;
    }
    settlementEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>Ref</th><th>User</th><th>Amount</th><th>Destination</th><th>Approved</th><th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${withdrawals.map((w) => `
            <tr data-payment-id="${w.id}">
              <td>${escapeHtml(w.our_ref)}</td>
              <td>${escapeHtml(w.display_name)} (#${w.user_id})</td>
              <td>${w.amount} ETB</td>
              <td>${escapeHtml(w.method_kind || "—")} / ${escapeHtml(w.account_ref || "—")} / ${escapeHtml(w.holder_name || "—")}</td>
              <td>${fmtDate(w.created_at)}</td>
              <td>
                <button class="btn btn-success btn-sm settle-btn">Mark sent</button>
                <button class="btn btn-danger btn-sm fail-btn">Fail (return funds)</button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
    for (const row of settlementEl.querySelectorAll("tr[data-payment-id]")) {
      const paymentId = Number(row.dataset.paymentId);
      row.querySelector(".settle-btn").addEventListener("click", () => settle(paymentId));
      row.querySelector(".fail-btn").addEventListener("click", () => fail(paymentId));
    }
  }

  async function approve(paymentId) {
    const reason = window.prompt("Reason to approve this withdrawal for manual payout:");
    if (reason === null) return;
    if (!reason.trim()) {
      toast("A reason is required.", true);
      return;
    }
    try {
      const result = await api(`/manual-withdrawals/${paymentId}/approve`, { method: "POST", body: { reason } });
      if (result.outcome === "awaiting_second_approval") {
        toast("First approval recorded -- a different admin must approve before the transfer can be sent.");
      } else {
        toast("Approved. Send the transfer, then settle it below once sent.");
      }
      reloadPending();
      reloadSettlement();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  async function reject(paymentId) {
    // Reuses the existing /withdrawals/{id}/reject route -- it already
    // reverses a manual withdrawal's locked funds correctly with no
    // provider-specific handling needed.
    const reason = window.prompt("Reason to reject this withdrawal:");
    if (reason === null) return;
    if (!reason.trim()) {
      toast("A reason is required.", true);
      return;
    }
    try {
      await api(`/withdrawals/${paymentId}/reject`, { method: "POST", body: { reason } });
      toast("Withdrawal rejected and funds returned.");
      reloadPending();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  async function settle(paymentId) {
    const externalReference = window.prompt("Real external reference number for the transfer you sent:");
    if (externalReference === null) return;
    if (!externalReference.trim()) {
      toast("An external reference is required.", true);
      return;
    }
    const reason = window.prompt("Reason / note for this settlement:");
    if (reason === null) return;
    if (!reason.trim()) {
      toast("A reason is required.", true);
      return;
    }
    try {
      await api(`/manual-withdrawals/${paymentId}/settle`, {
        method: "POST",
        body: { external_reference: externalReference, reason },
      });
      toast("Withdrawal settled.");
      reloadSettlement();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  async function fail(paymentId) {
    const reason = window.prompt("Reason the transfer could not be sent (funds will be returned):");
    if (reason === null) return;
    if (!reason.trim()) {
      toast("A reason is required.", true);
      return;
    }
    try {
      await api(`/manual-withdrawals/${paymentId}/fail`, { method: "POST", body: { reason } });
      toast("Marked failed and funds returned.");
      reloadSettlement();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  try {
    await Promise.all([reloadPending(), reloadSettlement()]);
  } catch (err) {
    renderError(pendingEl, err);
  }
}
