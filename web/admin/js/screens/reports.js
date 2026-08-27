import { api, escapeHtml } from "../api.js";
import { renderError } from "../ui.js";

export const label = "Reports";

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

export async function render(container) {
  container.innerHTML = `
    <h1>Reports</h1>

    <h2>Daily GGR</h2>
    <form id="ggr-form" class="inline-form">
      <input type="date" id="ggr-date" value="${todayIso()}" />
      <button type="submit" class="btn">Load</button>
    </form>
    <div id="ggr-result"></div>

    <h2>Top players by lifetime value</h2>
    <div id="ltv-result"><p class="loading">Loading…</p></div>

    <h2>Retention cohorts (last 8 signup weeks)</h2>
    <div id="retention-result"><p class="loading">Loading…</p></div>
  `;

  const ggrForm = container.querySelector("#ggr-form");
  const ggrDateInput = container.querySelector("#ggr-date");
  const ggrResult = container.querySelector("#ggr-result");
  const ltvResult = container.querySelector("#ltv-result");
  const retentionResult = container.querySelector("#retention-result");

  async function loadGgr() {
    ggrResult.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const data = await api(`/reports/ggr?on_date=${ggrDateInput.value}`);
      ggrResult.innerHTML = `
        <div class="stat-grid">
          <div class="stat-card"><div class="stat-label">GGR</div><div class="stat-value">${data.ggr} ETB</div></div>
          <div class="stat-card"><div class="stat-label">Rounds settled</div><div class="stat-value">${data.rounds_settled}</div></div>
        </div>
      `;
    } catch (err) {
      renderError(ggrResult, err);
    }
  }

  ggrForm.addEventListener("submit", (event) => {
    event.preventDefault();
    loadGgr();
  });

  async function loadLtv() {
    try {
      const players = await api("/reports/ltv?limit=20");
      if (players.length === 0) {
        ltvResult.innerHTML = `<p class="empty">No players with succeeded payments yet.</p>`;
        return;
      }
      ltvResult.innerHTML = `
        <table class="data-table">
          <thead><tr><th>User</th><th>Deposited</th><th>Withdrawn</th><th>Net LTV</th></tr></thead>
          <tbody>
            ${players.map((p) => `
              <tr>
                <td>${escapeHtml(p.display_name)} (#${p.user_id})</td>
                <td>${p.total_deposited} ETB</td>
                <td>${p.total_withdrawn} ETB</td>
                <td>${p.net_ltv} ETB</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
    } catch (err) {
      renderError(ltvResult, err);
    }
  }

  async function loadRetention() {
    try {
      const cohorts = await api("/reports/retention?weeks=8");
      if (cohorts.length === 0) {
        retentionResult.innerHTML = `<p class="empty">No signup cohorts yet.</p>`;
        return;
      }
      const maxOffset = Math.max(...cohorts.flatMap((c) => c.weeks.map((w) => w.week_offset)));
      retentionResult.innerHTML = `
        <table class="data-table">
          <thead>
            <tr>
              <th>Signup week</th><th>Size</th>
              ${Array.from({ length: maxOffset + 1 }, (_, i) => `<th>Week ${i}</th>`).join("")}
            </tr>
          </thead>
          <tbody>
            ${cohorts.map((c) => `
              <tr>
                <td>${c.cohort_week}</td>
                <td>${c.cohort_size}</td>
                ${c.weeks.map((w) => `
                  <td title="${w.active_users} active">
                    ${w.elapsed ? `${(w.retention_rate * 100).toFixed(1)}%` : "—"}
                  </td>
                `).join("")}
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
    } catch (err) {
      renderError(retentionResult, err);
    }
  }

  await Promise.all([loadGgr(), loadLtv(), loadRetention()]);
}
