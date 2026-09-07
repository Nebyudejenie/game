import { api } from "../api.js";
import { badge } from "../ui.js";

export const label = "Overview";

export async function render(container) {
  const data = await api("/overview");
  const messageTotal = Object.values(data.messages_by_status).reduce((a, b) => a + b, 0);
  const nodeTotal = Object.values(data.nodes_by_status).reduce((a, b) => a + b, 0);

  container.innerHTML = `
    <h1>Overview</h1>
    <h2>Messages</h2>
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-label">Total</div>
        <div class="stat-value">${messageTotal}</div>
      </div>
      ${Object.entries(data.messages_by_status).map(([status, n]) => `
        <div class="stat-card">
          <div class="stat-label">${badge(status)}</div>
          <div class="stat-value">${n}</div>
        </div>
      `).join("")}
    </div>

    <h2>Delivery nodes</h2>
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-label">Total</div>
        <div class="stat-value">${nodeTotal}</div>
      </div>
      ${Object.entries(data.nodes_by_status).map(([status, n]) => `
        <div class="stat-card">
          <div class="stat-label">${badge(status)}</div>
          <div class="stat-value">${n}</div>
        </div>
      `).join("")}
    </div>

    <h2>Campaigns</h2>
    <div class="stat-grid">
      ${Object.entries(data.campaigns_by_status).length === 0
        ? `<p class="empty">No campaigns yet.</p>`
        : Object.entries(data.campaigns_by_status).map(([status, n]) => `
          <div class="stat-card">
            <div class="stat-label">${badge(status)}</div>
            <div class="stat-value">${n}</div>
          </div>
        `).join("")}
    </div>
  `;
}
