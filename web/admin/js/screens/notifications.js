import { api, escapeHtml, fmtDate, parseMaybeJson } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Notifications";

const STATUS_VALUES = ["active", "limited", "self_excluded", "banned"];
const LANGUAGE_VALUES = ["am", "en", "om", "ti"];

// The only channel packages/core/campaigns.py and notification_templates'
// own CHECK constraint know about today -- see NOTIFICATION_CENTER_
// ARCHITECTURE.md for why email/SMS/push/in-app are NOT implemented.
const CHANNEL_LABEL = "Telegram";

function dateInputToIso(value) {
  if (!value) return null;
  return new Date(`${value}T00:00:00Z`).toISOString();
}

function readAudienceFilter(form) {
  const data = new FormData(form);
  const filter = {};
  if (data.get("status")) filter.status = data.get("status");
  if (data.get("language")) filter.language = data.get("language");
  if (data.get("min_kyc_level")) filter.min_kyc_level = Number(data.get("min_kyc_level"));
  if (data.get("registered_after")) filter.registered_after = dateInputToIso(data.get("registered_after"));
  if (data.get("registered_before")) filter.registered_before = dateInputToIso(data.get("registered_before"));
  if (data.get("active_since")) filter.active_since = dateInputToIso(data.get("active_since"));
  const userIdsRaw = (data.get("user_ids") || "").trim();
  if (userIdsRaw) {
    filter.user_ids = userIdsRaw.split(",").map((s) => s.trim()).filter(Boolean).map(Number);
  }
  return filter;
}

function readExcludeIds(form) {
  const raw = (new FormData(form).get("exclude_user_ids") || "").trim();
  if (!raw) return [];
  return raw.split(",").map((s) => s.trim()).filter(Boolean).map(Number);
}

const AUDIENCE_FIELDS_HTML = `
  <div class="detail-grid">
    <label>Status
      <select name="status">
        <option value="">Any</option>
        ${STATUS_VALUES.map((s) => `<option value="${s}">${s}</option>`).join("")}
      </select>
    </label>
    <label>Language
      <select name="language">
        <option value="">Any</option>
        ${LANGUAGE_VALUES.map((l) => `<option value="${l}">${l}</option>`).join("")}
      </select>
    </label>
    <label>Min KYC level <input type="number" name="min_kyc_level" min="0" max="2" /></label>
    <label>Registered after <input type="date" name="registered_after" /></label>
    <label>Registered before <input type="date" name="registered_before" /></label>
    <label>Active since <input type="date" name="active_since" /></label>
    <label>Specific user IDs <input type="text" name="user_ids" placeholder="Optional, comma-separated" /></label>
    <label>Exclude user IDs <input type="text" name="exclude_user_ids" placeholder="Optional, comma-separated" /></label>
  </div>
  <p class="wallet-note">Filled-in filters combine together (a recipient must match all of them). Leave everything
  blank to target every eligible player. Reaches Telegram only -- there is no email/SMS/push channel in this
  system. Self-excluded and banned players are never included, regardless of any filter above -- this can't
  be overridden from this screen.</p>
`;

export async function render(container) {
  container.innerHTML = `
    <h1>Notifications</h1>

    <h2>Overview (last 30 days)</h2>
    <div id="notif-overview"><p class="loading">Loading…</p></div>

    <h2>New campaign</h2>
    <form id="create-campaign-form" class="detail-panel">
      <div class="detail-grid">
        <label>Internal name <input type="text" name="internal_name" required placeholder="Not shown to players" /></label>
        <label>Template <select name="template_id"><option value="">None</option></select></label>
        <label>Title <input type="text" name="title" required /></label>
      </div>
      <label>Message body
        <textarea name="body" rows="3" required></textarea>
      </label>
      <h3>Audience</h3>
      ${AUDIENCE_FIELDS_HTML}
      <div class="action-row">
        <button type="button" class="btn btn-secondary" id="check-audience-btn">Check audience size</button>
        <span id="audience-count-result"></span>
      </div>
      <div class="action-row">
        <button type="submit" class="btn">Save as draft</button>
      </div>
    </form>

    <h2>Templates</h2>
    <div id="templates-list"><p class="loading">Loading…</p></div>
    <form id="create-template-form" class="detail-panel">
      <div class="detail-grid">
        <label>Name <input type="text" name="name" required /></label>
        <label>Category <input type="text" name="category" required /></label>
        <label>Title <input type="text" name="title" required /></label>
      </div>
      <label>Body <textarea name="body" rows="2" required></textarea></label>
      <div class="action-row">
        <button type="submit" class="btn btn-secondary">Save template</button>
      </div>
    </form>

    <h2>Campaigns</h2>
    <form id="campaign-filter-form" class="inline-form">
      <input type="text" id="campaign-search-input" placeholder="Search name or title" />
      <select id="campaign-status-select">
        <option value="">Any status</option>
        ${["draft", "scheduled", "queued", "sending", "completed", "partially_failed", "failed", "cancelled"]
          .map((s) => `<option value="${s}">${s}</option>`).join("")}
      </select>
      <button type="submit" class="btn">Filter</button>
    </form>
    <div id="campaigns-list"><p class="loading">Loading…</p></div>
    <div id="campaign-detail"></div>
  `;

  const overviewEl = container.querySelector("#notif-overview");
  const createForm = container.querySelector("#create-campaign-form");
  const templateSelect = createForm.querySelector("select[name=template_id]");
  const checkAudienceBtn = createForm.querySelector("#check-audience-btn");
  const audienceResultEl = createForm.querySelector("#audience-count-result");
  const templatesListEl = container.querySelector("#templates-list");
  const createTemplateForm = container.querySelector("#create-template-form");
  const filterForm = container.querySelector("#campaign-filter-form");
  const searchInput = container.querySelector("#campaign-search-input");
  const statusSelect = container.querySelector("#campaign-status-select");
  const campaignsListEl = container.querySelector("#campaigns-list");
  const detailEl = container.querySelector("#campaign-detail");

  // Cross-screen glue for the Bonuses & Referrals screen's "Announce this
  // rule" button (web/admin/js/screens/bonuses.js) -- sessionStorage
  // rather than a shared JS import, since each screen module is loaded
  // independently by app.js and neither needs to know the other exists
  // beyond this one seed.
  const draftSeed = sessionStorage.getItem("draftCampaignSeed");
  if (draftSeed) {
    sessionStorage.removeItem("draftCampaignSeed");
    try {
      const { internalName, title, body } = JSON.parse(draftSeed);
      createForm.querySelector("input[name=internal_name]").value = internalName || "";
      createForm.querySelector("input[name=title]").value = title || "";
      createForm.querySelector("textarea[name=body]").value = body || "";
    } catch {
      // Malformed seed -- ignore, the form just starts blank as usual.
    }
  }

  async function loadOverview() {
    overviewEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const data = await api("/notifications/overview");
      overviewEl.innerHTML = `
        <div class="stat-grid">
          <div class="stat-card"><div class="stat-label">Sent today</div><div class="stat-value">${data.sent_today}</div></div>
          <div class="stat-card"><div class="stat-label">Draft</div><div class="stat-value">${data.draft}</div></div>
          <div class="stat-card"><div class="stat-label">Scheduled</div><div class="stat-value">${data.scheduled}</div></div>
          <div class="stat-card"><div class="stat-label">Queued</div><div class="stat-value">${data.queued}</div></div>
          <div class="stat-card"><div class="stat-label">Sending</div><div class="stat-value">${data.sending}</div></div>
          <div class="stat-card"><div class="stat-label">Completed</div><div class="stat-value">${data.completed}</div></div>
          <div class="stat-card${data.partially_failed > 0 ? " stat-card-alert" : ""}"><div class="stat-label">Partially failed</div><div class="stat-value">${data.partially_failed}</div></div>
          <div class="stat-card${data.failed > 0 ? " stat-card-alert" : ""}"><div class="stat-label">Failed</div><div class="stat-value">${data.failed}</div></div>
          <div class="stat-card"><div class="stat-label">Delivered (30d)</div><div class="stat-value">${data.delivered_total_30d}</div></div>
          <div class="stat-card"><div class="stat-label">Delivery rate (30d)</div><div class="stat-value">${data.delivery_rate_30d === null ? "no data yet" : `${data.delivery_rate_30d}%`}</div></div>
        </div>
      `;
    } catch (err) {
      renderError(overviewEl, err);
    }
  }

  async function loadTemplateOptions() {
    try {
      const templates = await api("/notifications/templates");
      templateSelect.innerHTML = `<option value="">None</option>${templates
        .filter((t) => t.is_active)
        .map((t) => `<option value="${t.id}">${escapeHtml(t.name)}</option>`)
        .join("")}`;
      return templates;
    } catch {
      return [];
    }
  }

  async function loadTemplates() {
    templatesListEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const templates = await loadTemplateOptions();
      if (templates.length === 0) {
        templatesListEl.innerHTML = `<p class="empty">No templates yet.</p>`;
        return;
      }
      templatesListEl.innerHTML = `
        <table class="data-table">
          <thead><tr><th>Name</th><th>Category</th><th>Title</th><th>Channel</th><th>Active</th><th></th></tr></thead>
          <tbody>
            ${templates.map((t) => `
              <tr data-template-id="${t.id}">
                <td>${escapeHtml(t.name)}</td>
                <td>${escapeHtml(t.category)}</td>
                <td>${escapeHtml(t.title)}</td>
                <td>${CHANNEL_LABEL}</td>
                <td>${t.is_active ? "yes" : "no"}</td>
                <td><button class="btn btn-secondary btn-sm toggle-template-btn">${t.is_active ? "Deactivate" : "Activate"}</button></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
      for (const row of templatesListEl.querySelectorAll("tr[data-template-id]")) {
        const templateId = Number(row.dataset.templateId);
        const isActive = row.querySelector(".toggle-template-btn").textContent.trim() === "Deactivate";
        row.querySelector(".toggle-template-btn").addEventListener("click", async () => {
          try {
            await api(`/notifications/templates/${templateId}`, {
              method: "PATCH",
              body: { changes: { is_active: !isActive } },
            });
            toast("Template updated.");
            loadTemplates();
          } catch (err) {
            toast(err.detail || err.message, true);
          }
        });
      }
    } catch (err) {
      renderError(templatesListEl, err);
    }
  }

  createTemplateForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(createTemplateForm);
    try {
      await api("/notifications/templates", {
        method: "POST",
        body: {
          name: data.get("name"),
          category: data.get("category"),
          title: data.get("title"),
          body: data.get("body"),
        },
      });
      toast("Template saved.");
      createTemplateForm.reset();
      loadTemplates();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  });

  checkAudienceBtn.addEventListener("click", async () => {
    audienceResultEl.textContent = "Checking…";
    try {
      const result = await api("/notifications/audience/count", {
        method: "POST",
        body: {
          audience_filter: readAudienceFilter(createForm),
          exclude_user_ids: readExcludeIds(createForm),
        },
      });
      audienceResultEl.textContent = `${result.count} recipient${result.count === 1 ? "" : "s"} match this filter.`;
    } catch (err) {
      audienceResultEl.textContent = "";
      toast(err.detail || err.message, true);
    }
  });

  createForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(createForm);
    try {
      await api("/notifications/campaigns", {
        method: "POST",
        body: {
          internal_name: data.get("internal_name"),
          title: data.get("title"),
          body: data.get("body"),
          audience_filter: readAudienceFilter(createForm),
          exclude_user_ids: readExcludeIds(createForm),
          template_id: data.get("template_id") ? Number(data.get("template_id")) : null,
        },
      });
      toast("Draft campaign created below.");
      createForm.reset();
      audienceResultEl.textContent = "";
      loadCampaigns();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  });

  async function loadCampaigns() {
    campaignsListEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const params = new URLSearchParams();
      if (searchInput.value.trim()) params.set("search", searchInput.value.trim());
      if (statusSelect.value) params.set("status", statusSelect.value);
      const campaigns = await api(`/notifications/campaigns?${params.toString()}`);
      renderCampaigns(campaigns);
    } catch (err) {
      renderError(campaignsListEl, err);
    }
  }

  function renderCampaigns(campaigns) {
    if (campaigns.length === 0) {
      campaignsListEl.innerHTML = `<p class="empty">No campaigns yet.</p>`;
      return;
    }
    campaignsListEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr><th>Name</th><th>Status</th><th>Recipients</th><th>Delivered</th><th>Failed</th><th>Scheduled</th><th>Created by</th></tr>
        </thead>
        <tbody>
          ${campaigns.map((c) => `
            <tr class="clickable-row" data-campaign-id="${c.id}">
              <td>${escapeHtml(c.internal_name)}</td>
              <td><span class="badge badge-${c.status}">${c.status.replace("_", " ")}</span></td>
              <td>${c.recipient_count ?? "—"}</td>
              <td>${c.delivered_count}</td>
              <td>${c.failed_count}</td>
              <td>${fmtDate(c.scheduled_at)}</td>
              <td>${escapeHtml(c.created_by)}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
    for (const row of campaignsListEl.querySelectorAll(".clickable-row")) {
      row.addEventListener("click", () => loadDetail(Number(row.dataset.campaignId)));
    }
  }

  filterForm.addEventListener("submit", (event) => {
    event.preventDefault();
    loadCampaigns();
  });

  async function loadDetail(campaignId) {
    detailEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const [campaign, deliveries] = await Promise.all([
        api(`/notifications/campaigns/${campaignId}`),
        api(`/notifications/campaigns/${campaignId}/deliveries?limit=50`),
      ]);
      detailEl.innerHTML = renderDetail(campaign, deliveries);
      wireDetail(campaign);
    } catch (err) {
      renderError(detailEl, err);
    }
  }

  function renderDetail(campaign, deliveries) {
    const audience = parseMaybeJson(campaign.audience_filter);
    const audienceText = Object.keys(audience).length === 0
      ? "Every player"
      : escapeHtml(JSON.stringify(audience));
    return `
      <div class="detail-panel">
        <h2 style="margin-top:0">${escapeHtml(campaign.internal_name)} <span class="badge badge-${campaign.status}">${campaign.status.replace("_", " ")}</span></h2>
        <div class="detail-grid">
          <div><div class="field-label">Title</div><div class="field-value">${escapeHtml(campaign.title)}</div></div>
          <div><div class="field-label">Channel</div><div class="field-value">${CHANNEL_LABEL}</div></div>
          <div><div class="field-label">Audience</div><div class="field-value">${audienceText}</div></div>
          <div><div class="field-label">Recipients</div><div class="field-value">${campaign.recipient_count ?? "not resolved yet"}</div></div>
          <div><div class="field-label">Delivered / Failed</div><div class="field-value">${campaign.delivered_count} / ${campaign.failed_count}</div></div>
          <div><div class="field-label">Created by</div><div class="field-value">${escapeHtml(campaign.created_by)}</div></div>
          <div><div class="field-label">Scheduled</div><div class="field-value">${fmtDate(campaign.scheduled_at)}</div></div>
          <div><div class="field-label">Started</div><div class="field-value">${fmtDate(campaign.started_at)}</div></div>
          <div><div class="field-label">Completed</div><div class="field-value">${fmtDate(campaign.completed_at)}</div></div>
        </div>
        <div class="field-label">Message body</div>
        <pre class="code-block">${escapeHtml(campaign.body)}</pre>

        <div class="action-row" id="campaign-actions"></div>

        <h2>Deliveries</h2>
        ${deliveries.length === 0 ? '<p class="empty">No delivery records yet.</p>' : `
          <table class="data-table">
            <thead><tr><th>User</th><th>Status</th><th>Attempts</th><th>Failure reason</th><th>Delivered at</th></tr></thead>
            <tbody>
              ${deliveries.map((d) => `
                <tr>
                  <td>${escapeHtml(d.display_name)} (#${d.user_id})</td>
                  <td><span class="badge badge-${d.status}">${d.status}</span></td>
                  <td>${d.attempt_count}</td>
                  <td>${escapeHtml(d.failure_reason || "—")}</td>
                  <td>${fmtDate(d.delivered_at)}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        `}
      </div>
    `;
  }

  function wireDetail(campaign) {
    const actionsEl = detailEl.querySelector("#campaign-actions");
    const buttons = [];

    if (campaign.status === "draft") {
      buttons.push(`<button class="btn" id="send-now-btn">Send now</button>`);
      buttons.push(`<label>Schedule for <input type="datetime-local" id="schedule-input" /></label>`);
      buttons.push(`<button class="btn btn-secondary" id="schedule-btn">Schedule</button>`);
      buttons.push(`<button class="btn btn-danger" id="delete-btn">Delete draft</button>`);
    } else if (campaign.status === "scheduled") {
      buttons.push(`<label>Reschedule for <input type="datetime-local" id="schedule-input" /></label>`);
      buttons.push(`<button class="btn btn-secondary" id="schedule-btn">Reschedule</button>`);
      buttons.push(`<button class="btn btn-danger" id="cancel-btn">Cancel</button>`);
    } else if (campaign.status === "queued") {
      buttons.push(`<button class="btn btn-danger" id="cancel-btn">Cancel</button>`);
    }
    buttons.push(`<button class="btn btn-secondary" id="duplicate-btn">Duplicate</button>`);
    actionsEl.innerHTML = buttons.join(" ");

    const sendBtn = actionsEl.querySelector("#send-now-btn");
    if (sendBtn) {
      sendBtn.addEventListener("click", async () => {
        const count = campaign.recipient_count;
        const confirmMsg = `Send "${campaign.internal_name}" now${count != null ? ` to about ${count} recipients` : ""}? This cannot be undone.`;
        if (!window.confirm(confirmMsg)) return;
        try {
          await api(`/notifications/campaigns/${campaign.id}/send`, { method: "POST" });
          toast("Campaign queued for delivery.");
          loadDetail(campaign.id);
          loadCampaigns();
        } catch (err) {
          toast(err.detail || err.message, true);
        }
      });
    }

    const scheduleBtn = actionsEl.querySelector("#schedule-btn");
    if (scheduleBtn) {
      scheduleBtn.addEventListener("click", async () => {
        const input = actionsEl.querySelector("#schedule-input");
        if (!input.value) {
          toast("Pick a date and time first.", true);
          return;
        }
        try {
          await api(`/notifications/campaigns/${campaign.id}/schedule`, {
            method: "POST",
            body: { scheduled_at: new Date(input.value).toISOString() },
          });
          toast("Campaign scheduled.");
          loadDetail(campaign.id);
          loadCampaigns();
        } catch (err) {
          toast(err.detail || err.message, true);
        }
      });
    }

    const cancelBtn = actionsEl.querySelector("#cancel-btn");
    if (cancelBtn) {
      cancelBtn.addEventListener("click", async () => {
        if (!window.confirm(`Cancel "${campaign.internal_name}"?`)) return;
        try {
          await api(`/notifications/campaigns/${campaign.id}/cancel`, { method: "POST" });
          toast("Campaign cancelled.");
          loadDetail(campaign.id);
          loadCampaigns();
        } catch (err) {
          toast(err.detail || err.message, true);
        }
      });
    }

    const deleteBtn = actionsEl.querySelector("#delete-btn");
    if (deleteBtn) {
      deleteBtn.addEventListener("click", async () => {
        if (!window.confirm(`Delete draft "${campaign.internal_name}"? This cannot be undone.`)) return;
        try {
          await api(`/notifications/campaigns/${campaign.id}`, { method: "DELETE" });
          toast("Draft deleted.");
          detailEl.innerHTML = "";
          loadCampaigns();
        } catch (err) {
          toast(err.detail || err.message, true);
        }
      });
    }

    actionsEl.querySelector("#duplicate-btn").addEventListener("click", async () => {
      try {
        const result = await api(`/notifications/campaigns/${campaign.id}/duplicate`, { method: "POST" });
        toast("Duplicated as a new draft.");
        loadCampaigns();
        loadDetail(result.id);
      } catch (err) {
        toast(err.detail || err.message, true);
      }
    });
  }

  await Promise.all([loadOverview(), loadTemplates(), loadCampaigns()]);
}
