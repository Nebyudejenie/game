import { api, escapeHtml, fmtDate } from "../api.js";
import { badge, renderError, toast } from "../ui.js";

export const label = "Campaigns";

export async function render(container) {
  container.innerHTML = `
    <h1>Campaigns</h1>
    <form id="create-campaign-form" class="inline-form">
      <input type="text" name="name" placeholder="Campaign name" required />
      <input type="text" name="body_override" placeholder="Message text ({{display_name}} allowed)" required />
      <input type="text" name="attribute_key" placeholder="Audience: attribute key (optional)" />
      <input type="text" name="attribute_value" placeholder="Audience: attribute value" />
      <button type="submit" class="btn">Create draft</button>
    </form>
    <div id="campaigns-error"></div>
    <table class="data-table">
      <thead><tr><th>ID</th><th>Name</th><th>Status</th><th>Recipients</th><th>Started</th></tr></thead>
      <tbody id="campaigns-body"><tr><td colspan="5" class="loading">Loading…</td></tr></tbody>
    </table>
    <div id="campaign-detail"></div>
  `;

  const errorEl = container.querySelector("#campaigns-error");
  const bodyEl = container.querySelector("#campaigns-body");
  const detailEl = container.querySelector("#campaign-detail");

  async function refreshList() {
    const campaigns = await api("/campaigns");
    bodyEl.innerHTML = campaigns.length === 0
      ? `<tr><td colspan="5" class="empty">No campaigns yet.</td></tr>`
      : campaigns.map((c) => `
        <tr class="clickable-row" data-id="${c.id}">
          <td>${c.id}</td><td>${escapeHtml(c.name)}</td><td>${badge(c.status)}</td>
          <td>${c.recipient_count ?? "—"}</td><td>${fmtDate(c.started_at)}</td>
        </tr>
      `).join("");
    for (const row of bodyEl.querySelectorAll("tr[data-id]")) {
      row.addEventListener("click", () => showDetail(Number(row.dataset.id)));
    }
  }

  async function showDetail(campaignId) {
    detailEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const [campaign, messages] = await Promise.all([
        api(`/campaigns/${campaignId}`),
        api(`/campaigns/${campaignId}/messages`),
      ]);
      const byStatus = {};
      for (const m of messages) byStatus[m.status] = (byStatus[m.status] || 0) + 1;

      detailEl.innerHTML = `
        <div class="detail-panel">
          <div class="detail-grid">
            <div><div class="field-label">Status</div><div class="field-value">${badge(campaign.status)}</div></div>
            <div><div class="field-label">Recipients</div><div class="field-value">${campaign.recipient_count ?? "—"}</div></div>
            <div><div class="field-label">Audience filter</div><div class="field-value">${escapeHtml(JSON.stringify(campaign.audience_filter))}</div></div>
          </div>
          <div class="action-row" id="campaign-actions"></div>
          <h2>Messages (${messages.length})</h2>
          <div class="stat-grid">
            ${Object.entries(byStatus).map(([s, n]) => `
              <div class="stat-card"><div class="stat-label">${badge(s)}</div><div class="stat-value">${n}</div></div>
            `).join("") || `<p class="empty">No messages yet -- start the campaign to enqueue them.</p>`}
          </div>
        </div>
      `;
      renderActions(campaign);
    } catch (err) {
      renderError(detailEl, err);
    }
  }

  function renderActions(campaign) {
    const actionsEl = detailEl.querySelector("#campaign-actions");
    const buttons = [];
    if (campaign.status === "draft" || campaign.status === "failed") {
      buttons.push(["validate", "Validate", "btn"]);
    }
    if (campaign.status === "ready" || campaign.status === "scheduled") {
      buttons.push(["start", "Start sending", "btn btn-success"]);
    }
    if (campaign.status === "running") {
      buttons.push(["pause", "Pause", "btn btn-secondary"]);
    }
    if (campaign.status === "paused") {
      buttons.push(["resume", "Resume", "btn btn-success"]);
    }
    if (["draft", "ready", "scheduled", "running", "paused"].includes(campaign.status)) {
      buttons.push(["cancel", "Cancel", "btn btn-danger"]);
    }
    actionsEl.innerHTML = buttons.map(([action, text, cls]) => `<button class="${cls}" data-action="${action}">${text}</button>`).join("");
    for (const btn of actionsEl.querySelectorAll("button")) {
      btn.addEventListener("click", () => runAction(campaign.id, btn.dataset.action));
    }
  }

  async function runAction(campaignId, action) {
    try {
      if (action === "cancel") {
        const reason = window.prompt("Reason for cancelling this campaign?");
        if (!reason) return;
        await api(`/campaigns/${campaignId}/cancel`, { method: "POST", body: { reason } });
      } else {
        await api(`/campaigns/${campaignId}/${action}`, { method: "POST" });
      }
      toast(`Campaign ${action} succeeded`);
      await refreshList();
      await showDetail(campaignId);
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  container.querySelector("#create-campaign-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    errorEl.innerHTML = "";
    const data = new FormData(event.target);
    const audienceFilter = {};
    if (data.get("attribute_key")) {
      audienceFilter.attributes = { [data.get("attribute_key")]: data.get("attribute_value") };
    }
    try {
      await api("/campaigns", {
        method: "POST",
        body: { name: data.get("name"), body_override: data.get("body_override"), audience_filter: audienceFilter },
      });
      event.target.reset();
      toast("Draft campaign created");
      await refreshList();
    } catch (err) {
      renderError(errorEl, err);
    }
  });

  await refreshList();
}
