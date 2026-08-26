import { api, escapeHtml } from "../api.js";

export const label = "Risk";

export async function render(container) {
  container.innerHTML = `
    <h1>Risk</h1>
    <p class="empty">
      Data screens for investigation, not automatic verdicts -- deciding
      what actually looks suspicious here is a judgment call for the
      admin reviewing it.
    </p>

    <h2>Shared payout account clusters</h2>
    <p class="empty">Multiple accounts registered with the same withdrawal destination.</p>
    <div id="clusters-result"><p class="loading">Loading…</p></div>

    <h2>Repeat winner/loser room pairings</h2>
    <p class="empty">Pairs of users who have repeatedly shared a round, and how often each side won.</p>
    <div id="pairings-result"><p class="loading">Loading…</p></div>
  `;

  const clustersEl = container.querySelector("#clusters-result");
  const pairingsEl = container.querySelector("#pairings-result");

  async function loadClusters() {
    try {
      const clusters = await api("/risk/shared-payout-accounts");
      if (clusters.length === 0) {
        clustersEl.innerHTML = `<p class="empty">No shared payout destinations found.</p>`;
        return;
      }
      clustersEl.innerHTML = `
        <table class="data-table">
          <thead><tr><th>Account ref</th><th>Method</th><th>Users</th></tr></thead>
          <tbody>
            ${clusters.map((c) => `
              <tr>
                <td>${escapeHtml(c.account_ref)}</td>
                <td>${escapeHtml(c.kind)}</td>
                <td>${c.users.map((u) => `${escapeHtml(u.display_name)} (#${u.user_id})`).join(", ")}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
    } catch (err) {
      clustersEl.innerHTML = `<p class="error-banner">${escapeHtml(err.detail || err.message)}</p>`;
    }
  }

  async function loadPairings() {
    try {
      const pairings = await api("/risk/repeat-pairings?min_shared_rounds=3&since_days=30");
      if (pairings.length === 0) {
        pairingsEl.innerHTML = `<p class="empty">No recurring pairs found in the last 30 days.</p>`;
        return;
      }
      pairingsEl.innerHTML = `
        <table class="data-table">
          <thead><tr><th>Pair</th><th>Shared rounds</th><th>Win split</th></tr></thead>
          <tbody>
            ${pairings.map((p) => `
              <tr>
                <td>${escapeHtml(p.user_a_name)} (#${p.user_a}) vs ${escapeHtml(p.user_b_name)} (#${p.user_b})</td>
                <td>${p.shared_rounds}</td>
                <td>${p.user_a_wins} – ${p.user_b_wins}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
    } catch (err) {
      pairingsEl.innerHTML = `<p class="error-banner">${escapeHtml(err.detail || err.message)}</p>`;
    }
  }

  await Promise.all([loadClusters(), loadPairings()]);
}
