import { api, escapeHtml } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Templates";

export async function render(container) {
  container.innerHTML = `
    <h1>Templates</h1>
    <form id="create-template-form" class="inline-form">
      <input type="text" name="name" placeholder="Template name" required />
      <input type="text" name="body" placeholder="Hi {{display_name}}, ..." required />
      <button type="submit" class="btn">Create</button>
    </form>
    <div id="templates-error"></div>
    <table class="data-table">
      <thead><tr><th>Name</th><th>Body</th><th>Variables</th></tr></thead>
      <tbody id="templates-body"><tr><td colspan="3" class="loading">Loading…</td></tr></tbody>
    </table>
  `;

  const errorEl = container.querySelector("#templates-error");
  const bodyEl = container.querySelector("#templates-body");

  async function refresh() {
    const templates = await api("/templates");
    bodyEl.innerHTML = templates.length === 0
      ? `<tr><td colspan="3" class="empty">No templates yet.</td></tr>`
      : templates.map((t) => `
        <tr><td>${escapeHtml(t.name)}</td><td>${escapeHtml(t.body)}</td><td>${(t.variables || []).map(escapeHtml).join(", ") || "—"}</td></tr>
      `).join("");
  }

  container.querySelector("#create-template-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    errorEl.innerHTML = "";
    const data = new FormData(event.target);
    try {
      await api("/templates", { method: "POST", body: { name: data.get("name"), body: data.get("body") } });
      event.target.reset();
      toast("Template created");
      await refresh();
    } catch (err) {
      renderError(errorEl, err);
    }
  });

  await refresh();
}
