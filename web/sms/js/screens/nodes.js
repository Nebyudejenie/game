import { api, escapeHtml, fmtDate } from "../api.js";
import { badge, renderError, toast } from "../ui.js";

export const label = "Delivery Nodes";

export async function render(container) {
  container.innerHTML = `
    <h1>Delivery Nodes</h1>
    <form id="create-node-form" class="inline-form">
      <input type="text" name="name" placeholder="Node name (e.g. android-01)" required />
      <input type="text" name="fleet_group" placeholder="Fleet group" value="default" />
      <button type="submit" class="btn">Register node</button>
    </form>
    <div id="nodes-error"></div>
    <div id="new-token-panel"></div>
    <table class="data-table">
      <thead><tr><th>Name</th><th>Fleet</th><th>Status</th><th>Capacity</th><th>Protocol</th><th>Health</th><th>Last heartbeat</th><th>Actions</th></tr></thead>
      <tbody id="nodes-body"><tr><td colspan="8" class="loading">Loading…</td></tr></tbody>
    </table>
  `;

  const errorEl = container.querySelector("#nodes-error");
  const bodyEl = container.querySelector("#nodes-body");
  const tokenPanel = container.querySelector("#new-token-panel");

  // Keyed by the *stored* status -- 'degraded'/'offline' are display-only
  // overlays computed server-side (never stored), so they never appear as
  // a key here; an active-but-degraded node still gets active's actions.
  const ACTIONS = {
    pending: [["approve", "Approve"], ["revoke", "Revoke"]],
    active: [["disable", "Disable"], ["maintenance", "Maintenance"], ["drain", "Drain"], ["revoke", "Revoke"]],
    maintenance: [["resume", "Resume"], ["revoke", "Revoke"]],
    disabled: [["resume", "Resume"], ["revoke", "Revoke"]],
    draining: [["resume", "Resume"], ["revoke", "Revoke"]],
    revoked: [],
  };

  async function refresh() {
    const nodes = await api("/nodes");
    bodyEl.innerHTML = nodes.length === 0
      ? `<tr><td colspan="8" class="empty">No nodes registered yet.</td></tr>`
      : nodes.map((n) => `
        <tr data-id="${n.id}">
          <td>${escapeHtml(n.name)}</td>
          <td>${escapeHtml(n.fleet_group)}</td>
          <td>${badge(n.display_status)}</td>
          <td>${n.max_concurrent_jobs}</td>
          <td>v${n.protocol_version}</td>
          <td><span class="health-bar"><span class="health-bar-fill" style="width:${n.health_score}%"></span></span> ${n.health_score}</td>
          <td>${fmtDate(n.last_heartbeat_at)}</td>
          <td>
            ${(ACTIONS[n.status] || []).map(([action, text]) => `<button class="btn btn-secondary" data-action="${action}" data-id="${n.id}">${text}</button>`).join(" ")}
            <button class="btn btn-secondary" data-action="rotate-token" data-id="${n.id}">Rotate token</button>
          </td>
        </tr>
      `).join("");
    for (const btn of bodyEl.querySelectorAll("button[data-action]")) {
      btn.addEventListener("click", () => runAction(Number(btn.dataset.id), btn.dataset.action));
    }
  }

  async function runAction(nodeId, action) {
    try {
      if (action === "rotate-token") {
        const result = await api(`/nodes/${nodeId}/rotate-token`, { method: "POST" });
        showToken("Rotated credential (shown once)", result.token);
      } else {
        await api(`/nodes/${nodeId}/${action}`, { method: "POST" });
        toast(`Node ${action} succeeded`);
      }
      await refresh();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  function showToken(title, token) {
    tokenPanel.innerHTML = `
      <div class="detail-panel">
        <div class="field-label">${escapeHtml(title)} -- copy it now, it will not be shown again</div>
        <pre class="code-block">${escapeHtml(token)}</pre>
      </div>
    `;
  }

  container.querySelector("#create-node-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    errorEl.innerHTML = "";
    const data = new FormData(event.target);
    try {
      const result = await api("/nodes", {
        method: "POST",
        body: { name: data.get("name"), fleet_group: data.get("fleet_group") || "default" },
      });
      event.target.reset();
      showToken(`Node "${result.name}" registered`, result.token);
      await refresh();
    } catch (err) {
      renderError(errorEl, err);
    }
  });

  await refresh();
}
