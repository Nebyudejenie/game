import { api, escapeHtml, fmtDate } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Users";

const STATUSES = ["active", "limited", "banned"];
const KYC_LEVELS = [0, 1, 2];

export async function render(container) {
  container.innerHTML = `
    <h1>Users</h1>
    <form id="user-search-form" class="inline-form">
      <input type="text" id="user-search-input" placeholder="Name, exact phone, or Telegram ID" required />
      <button type="submit" class="btn">Search</button>
    </form>
    <div id="user-results"></div>
    <div id="user-detail"></div>
  `;

  const form = container.querySelector("#user-search-form");
  const input = container.querySelector("#user-search-input");
  const resultsEl = container.querySelector("#user-results");
  const detailEl = container.querySelector("#user-detail");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    resultsEl.innerHTML = `<p class="loading">Searching…</p>`;
    detailEl.innerHTML = "";
    try {
      const users = await api(`/users?q=${encodeURIComponent(q)}`);
      renderResults(users);
    } catch (err) {
      renderError(resultsEl, err);
    }
  });

  function renderResults(users) {
    if (users.length === 0) {
      resultsEl.innerHTML = `<p class="empty">No users found.</p>`;
      return;
    }
    resultsEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr><th>ID</th><th>Name</th><th>Phone</th><th>Status</th><th>KYC</th><th>Joined</th></tr>
        </thead>
        <tbody>
          ${users.map((u) => `
            <tr class="clickable-row" data-user-id="${u.id}">
              <td>${u.id}</td>
              <td>${escapeHtml(u.display_name)}</td>
              <td>${escapeHtml(u.phone_e164 || "—")}</td>
              <td><span class="badge badge-${escapeHtml(u.status)}">${escapeHtml(u.status)}</span></td>
              <td>${u.kyc_level}</td>
              <td>${fmtDate(u.created_at)}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
    for (const row of resultsEl.querySelectorAll(".clickable-row")) {
      row.addEventListener("click", () => loadDetail(Number(row.dataset.userId)));
    }
  }

  async function loadDetail(userId) {
    detailEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const [user, ledger] = await Promise.all([
        api(`/users/${userId}`),
        api(`/users/${userId}/ledger`),
      ]);
      detailEl.innerHTML = renderDetail(user, ledger);
      wireActions(user);
    } catch (err) {
      renderError(detailEl, err);
    }
  }

  function renderDetail(user, ledger) {
    return `
      <div class="detail-panel">
        <h2 style="margin-top:0">${escapeHtml(user.display_name)} <span class="badge badge-${escapeHtml(user.status)}">${escapeHtml(user.status)}</span></h2>
        <div class="detail-grid">
          <div><div class="field-label">User ID</div><div class="field-value">${user.id}</div></div>
          <div><div class="field-label">Telegram ID</div><div class="field-value">${user.telegram_id}</div></div>
          <div><div class="field-label">Phone</div><div class="field-value">${escapeHtml(user.phone_e164 || "—")}</div></div>
          <div><div class="field-label">KYC level</div><div class="field-value">${user.kyc_level}</div></div>
          <div><div class="field-label">Cash</div><div class="field-value">${user.balances.cash} ETB</div></div>
          <div><div class="field-label">Bonus</div><div class="field-value">${user.balances.bonus} ETB</div></div>
          <div><div class="field-label">Locked</div><div class="field-value">${user.balances.locked} ETB</div></div>
          <div><div class="field-label">Net LTV</div><div class="field-value">${user.ltv.net_ltv} ETB</div></div>
          <div><div class="field-label">Last seen</div><div class="field-value">${fmtDate(user.last_seen_at)}</div></div>
        </div>

        <div class="action-row">
          <label>Adjust balance (ETB, negative to debit)
            <input type="text" id="adjust-amount" placeholder="e.g. 50.00 or -20.00" />
          </label>
          <label>Reason
            <input type="text" id="adjust-reason" placeholder="required" />
          </label>
          <button class="btn" id="adjust-submit">Apply</button>
        </div>

        <div class="action-row">
          <label>Set status
            <select id="status-select">
              ${STATUSES.map((s) => `<option value="${s}" ${s === user.status ? "selected" : ""}>${s}</option>`).join("")}
            </select>
          </label>
          <label>Reason
            <input type="text" id="status-reason" placeholder="required" />
          </label>
          <button class="btn" id="status-submit">Apply</button>
        </div>

        <div class="action-row">
          <label>Set KYC level
            <select id="kyc-select">
              ${KYC_LEVELS.map((k) => `<option value="${k}" ${k === user.kyc_level ? "selected" : ""}>${k}</option>`).join("")}
            </select>
          </label>
          <label>Reason
            <input type="text" id="kyc-reason" placeholder="e.g. ID documents reviewed and verified" />
          </label>
          <button class="btn" id="kyc-submit">Apply</button>
        </div>

        <h2>Recent ledger activity</h2>
        ${ledger.length === 0 ? '<p class="empty">No ledger entries.</p>' : `
          <table class="data-table">
            <thead><tr><th>When</th><th>Account</th><th>Kind</th><th>Amount</th><th>Memo</th></tr></thead>
            <tbody>
              ${ledger.map((e) => `
                <tr>
                  <td>${fmtDate(e.created_at)}</td>
                  <td>${escapeHtml(e.account_kind)}</td>
                  <td>${escapeHtml(e.kind)}</td>
                  <td>${e.amount}</td>
                  <td>${escapeHtml(e.memo || "—")}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        `}
      </div>
    `;
  }

  function wireActions(user) {
    const panel = detailEl.querySelector(".detail-panel");

    panel.querySelector("#adjust-submit").addEventListener("click", async () => {
      const amount = panel.querySelector("#adjust-amount").value.trim();
      const reason = panel.querySelector("#adjust-reason").value.trim();
      if (!amount || !reason) {
        toast("Amount and reason are both required.", true);
        return;
      }
      // Disabling on click closes the common double-click case outright;
      // the fresh request_id per click is what the backend actually keys
      // its idempotency check on, so a retried request (not just a literal
      // double-click) still can't create a second real-money transaction.
      const submitBtn = panel.querySelector("#adjust-submit");
      submitBtn.disabled = true;
      try {
        await api(`/users/${user.id}/adjust`, {
          method: "POST",
          body: { amount, reason, request_id: crypto.randomUUID() },
        });
        toast("Balance adjusted.");
        loadDetail(user.id);
      } catch (err) {
        toast(err.detail || err.message, true);
      } finally {
        submitBtn.disabled = false;
      }
    });

    panel.querySelector("#status-submit").addEventListener("click", async () => {
      const status = panel.querySelector("#status-select").value;
      const reason = panel.querySelector("#status-reason").value.trim();
      if (!reason) {
        toast("Reason is required.", true);
        return;
      }
      try {
        await api(`/users/${user.id}/status`, { method: "POST", body: { status, reason } });
        toast("Status updated.");
        loadDetail(user.id);
      } catch (err) {
        toast(err.detail || err.message, true);
      }
    });

    panel.querySelector("#kyc-submit").addEventListener("click", async () => {
      const kyc_level = Number(panel.querySelector("#kyc-select").value);
      const reason = panel.querySelector("#kyc-reason").value.trim();
      if (!reason) {
        toast("Reason is required.", true);
        return;
      }
      try {
        await api(`/users/${user.id}/kyc`, { method: "POST", body: { kyc_level, reason } });
        toast("KYC level updated.");
        loadDetail(user.id);
      } catch (err) {
        toast(err.detail || err.message, true);
      }
    });
  }
}
