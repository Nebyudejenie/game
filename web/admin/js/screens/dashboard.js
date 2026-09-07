import { api, escapeHtml } from "../api.js";

export const label = "Dashboard";

const HEALTH_BADGE = { green: "active", amber: "review", red: "banned", unknown: "expired" };
const HEALTH_LABEL = { database: "Database", redis: "Redis", telegram: "Telegram", bingo: "Bingo" };

export async function render(container) {
  const data = await api("/dashboard");
  container.innerHTML = `
    <h1>Dashboard</h1>
    <div id="system-health-row"><p class="loading">Checking system health…</p></div>
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-label">Active rounds</div>
        <div class="stat-value">${data.active_rounds}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Active rooms</div>
        <div class="stat-value">${data.active_rooms}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Stakes today</div>
        <div class="stat-value">${data.stakes_today} ETB</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Payouts today</div>
        <div class="stat-value">${data.payouts_today} ETB</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">House revenue today</div>
        <div class="stat-value">${data.house_revenue_today} ETB</div>
      </div>
      <div class="stat-card${data.pending_withdrawals_count > 0 ? " stat-card-alert" : ""}">
        <div class="stat-label">Withdrawals awaiting review</div>
        <div class="stat-value">${data.pending_withdrawals_count}</div>
      </div>
    </div>
  `;

  const healthEl = container.querySelector("#system-health-row");
  try {
    const checks = await api("/system-health");
    healthEl.innerHTML = `
      <div class="detail-grid" style="margin-bottom:1rem">
        ${checks.map((c) => `
          <div title="${escapeHtml(c.why)}">
            <span class="badge badge-${HEALTH_BADGE[c.status] || "expired"}">${HEALTH_LABEL[c.name] || escapeHtml(c.name)}</span>
          </div>
        `).join("")}
      </div>
    `;
  } catch {
    healthEl.innerHTML = `<p class="empty">System health check unavailable.</p>`;
  }
}
