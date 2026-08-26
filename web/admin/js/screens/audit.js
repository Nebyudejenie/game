import { api, escapeHtml, fmtDate, parseMaybeJson } from "../api.js";

export const label = "Audit log";

export async function render(container) {
  container.innerHTML = `
    <h1>Audit log</h1>
    <p class="empty">Every admin mutation, immutable at the database level (append-only, no update/delete grant).</p>
    <div id="audit-result"><p class="loading">Loading…</p></div>
  `;

  const resultEl = container.querySelector("#audit-result");
  try {
    const rows = await api("/audit-log?limit=100");
    if (rows.length === 0) {
      resultEl.innerHTML = `<p class="empty">No admin actions recorded yet.</p>`;
      return;
    }
    resultEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr><th>When</th><th>Admin</th><th>Action</th><th>Target</th><th>Before → After</th><th>Reason</th></tr>
        </thead>
        <tbody>
          ${rows.map((r) => `
            <tr>
              <td>${fmtDate(r.created_at)}</td>
              <td>#${r.admin_id}</td>
              <td>${escapeHtml(r.action)}</td>
              <td>${escapeHtml(r.target_type)} #${escapeHtml(r.target_id)}</td>
              <td>
                <pre class="code-block">${escapeHtml(JSON.stringify(parseMaybeJson(r.before)))} →
${escapeHtml(JSON.stringify(parseMaybeJson(r.after)))}</pre>
              </td>
              <td>${escapeHtml(r.reason || "—")}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
  } catch (err) {
    resultEl.innerHTML = `<p class="error-banner">${escapeHtml(err.detail || err.message)}</p>`;
  }
}
