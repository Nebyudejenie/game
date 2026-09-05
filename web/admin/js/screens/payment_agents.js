import { api, escapeHtml, fmtDate } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Payment Agents";

export async function render(container) {
  container.innerHTML = `
    <h1>Telegram payment agents</h1>
    <p class="wallet-note">
      Telegram accounts allowed to forward Telebirr SMS text to the bot for automatic ingestion
      (services/bot/handlers.py's on_agent_sms). Ships empty -- add a real Telegram user id below to
      authorize someone.
    </p>
    <div id="agents-list"><p class="loading">Loading…</p></div>

    <h2>Add agent</h2>
    <form id="create-agent-form" class="detail-panel">
      <div class="detail-grid">
        <label>Telegram user id <input type="number" name="telegram_user_id" required /></label>
        <label>Display name <input type="text" name="display_name" placeholder="Optional" /></label>
      </div>
      <div class="action-row">
        <button type="submit" class="btn">Add agent</button>
      </div>
    </form>
  `;

  const listEl = container.querySelector("#agents-list");
  const createForm = container.querySelector("#create-agent-form");

  async function reload() {
    listEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const agents = await api("/payment-agents");
      renderList(agents);
    } catch (err) {
      renderError(listEl, err);
    }
  }

  function renderList(agents) {
    if (agents.length === 0) {
      listEl.innerHTML = `<p class="empty">No payment agents configured yet.</p>`;
      return;
    }
    listEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr><th>Telegram user id</th><th>Display name</th><th>Active</th><th>Added</th><th>Submissions</th><th>Last submission</th><th></th></tr>
        </thead>
        <tbody>
          ${agents.map((a) => `
            <tr data-agent-id="${a.id}">
              <td>${a.telegram_user_id}</td>
              <td>${escapeHtml(a.display_name || "—")}</td>
              <td>${a.is_active ? "yes" : "no"}</td>
              <td>${fmtDate(a.created_at)}</td>
              <td>${a.submission_count}</td>
              <td>${fmtDate(a.last_submission_at)}</td>
              <td>
                <button class="btn btn-secondary btn-sm toggle-active-btn">${a.is_active ? "Deactivate" : "Activate"}</button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
    for (const row of listEl.querySelectorAll("tr[data-agent-id]")) {
      const agentId = Number(row.dataset.agentId);
      const isActive = row.querySelector(".toggle-active-btn").textContent.trim() === "Deactivate";
      row.querySelector(".toggle-active-btn").addEventListener("click", () => toggleActive(agentId, isActive));
    }
  }

  async function toggleActive(agentId, currentlyActive) {
    try {
      await api(`/payment-agents/${agentId}`, { method: "PATCH", body: { is_active: !currentlyActive } });
      toast("Agent updated.");
      reload();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  createForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(createForm);
    const telegramUserId = Number(data.get("telegram_user_id"));
    if (!Number.isInteger(telegramUserId) || telegramUserId <= 0) {
      toast("Please enter a real Telegram user id.", true);
      return;
    }
    try {
      await api("/payment-agents", {
        method: "POST",
        body: { telegram_user_id: telegramUserId, display_name: data.get("display_name") || null },
      });
      toast("Agent added.");
      createForm.reset();
      reload();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  });

  await reload();
}
