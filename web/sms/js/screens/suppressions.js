import { api, escapeHtml, fmtDate } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Suppressions";

export async function render(container) {
  container.innerHTML = `
    <h1>Suppressions</h1>
    <p style="color:var(--text-dim);font-size:0.85rem">
      A suppressed phone number is excluded from every campaign's audience automatically -- see
      packages/core/sms/audience.py. This list is admin-curated in this pass; there is no
      automatic inbound STOP-keyword detection yet.
    </p>
    <form id="add-suppression-form" class="inline-form">
      <input type="text" name="phone_e164" placeholder="+251911000000" required />
      <select name="reason">
        <option value="manual">Manual</option>
        <option value="opt_out">Opt-out</option>
        <option value="bounce">Bounce</option>
        <option value="complaint">Complaint</option>
      </select>
      <input type="text" name="note" placeholder="Note (optional)" />
      <button type="submit" class="btn btn-danger">Suppress</button>
    </form>
    <div id="suppressions-error"></div>
    <table class="data-table">
      <thead><tr><th>Phone</th><th>Reason</th><th>Note</th><th>Added</th><th></th></tr></thead>
      <tbody id="suppressions-body"><tr><td colspan="5" class="loading">Loading…</td></tr></tbody>
    </table>
  `;

  const errorEl = container.querySelector("#suppressions-error");
  const bodyEl = container.querySelector("#suppressions-body");

  async function refresh() {
    const rows = await api("/suppressions");
    bodyEl.innerHTML = rows.length === 0
      ? `<tr><td colspan="5" class="empty">No suppressions.</td></tr>`
      : rows.map((s) => `
        <tr>
          <td>${escapeHtml(s.phone_e164)}</td><td>${escapeHtml(s.reason)}</td>
          <td>${escapeHtml(s.note || "—")}</td><td>${fmtDate(s.created_at)}</td>
          <td><button class="btn btn-secondary" data-phone="${escapeHtml(s.phone_e164)}">Remove</button></td>
        </tr>
      `).join("");
    for (const btn of bodyEl.querySelectorAll("button[data-phone]")) {
      btn.addEventListener("click", async () => {
        await api(`/suppressions/${encodeURIComponent(btn.dataset.phone)}`, { method: "DELETE" });
        toast("Suppression removed");
        await refresh();
      });
    }
  }

  container.querySelector("#add-suppression-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    errorEl.innerHTML = "";
    const data = new FormData(event.target);
    try {
      await api("/suppressions", {
        method: "POST",
        body: { phone_e164: data.get("phone_e164"), reason: data.get("reason"), note: data.get("note") || null },
      });
      event.target.reset();
      toast("Number suppressed");
      await refresh();
    } catch (err) {
      renderError(errorEl, err);
    }
  });

  await refresh();
}
