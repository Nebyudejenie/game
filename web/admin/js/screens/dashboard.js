import { api } from "../api.js";

export const label = "Dashboard";

export async function render(container) {
  const data = await api("/dashboard");
  container.innerHTML = `
    <h1>Dashboard</h1>
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
    </div>
  `;
}
