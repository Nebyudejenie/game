import { api, escapeHtml, fmtDate, parseMaybeJson } from "../api.js";
import { renderError } from "../ui.js";

export const label = "Audit log";

export async function render(container) {
  container.innerHTML = `
    <h1>Audit log</h1>
    <p class="empty">Every admin mutation, immutable at the database level (append-only, no update/delete grant).</p>
    <form id="audit-filter-form" class="inline-form">
      <input type="number" id="audit-admin-id-input" placeholder="Filter by admin ID (optional)" />
      <input type="text" id="audit-action-input" placeholder="Filter by action, e.g. admin_users.create (optional)" />
      <button type="submit" class="btn">Filter</button>
    </form>
    <div id="audit-result"><p class="loading">Loading…</p></div>
  `;

  const form = container.querySelector("#audit-filter-form");
  const adminIdInput = container.querySelector("#audit-admin-id-input");
  const actionInput = container.querySelector("#audit-action-input");
  const resultEl = container.querySelector("#audit-result");

  async function load() {
    resultEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const params = new URLSearchParams({ limit: "100" });
      if (adminIdInput.value.trim()) params.set("admin_id", adminIdInput.value.trim());
      if (actionInput.value.trim()) params.set("action", actionInput.value.trim());
      const rows = await api(`/audit-log?${params.toString()}`);
      renderRows(rows);
    } catch (err) {
      renderError(resultEl, err);
    }
  }

  function renderRows(rows) {
    if (rows.length === 0) {
      resultEl.innerHTML = `<p class="empty">No admin actions match this filter.</p>`;
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
              <td>${escapeHtml(r.admin_username)} (#${r.admin_id})</td>
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
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    load();
  });

  await load();
}
