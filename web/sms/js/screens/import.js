// Bulk SMS CSV import: upload -> validate -> preview -> confirm ->
// create campaign. The created campaign is an ordinary sms_campaigns
// row from that point on -- Validate/Start/Pause/Cancel all already
// work unchanged from the Campaigns screen, so this screen's only job
// is getting a CSV turned into a draft campaign safely.

import { api, escapeHtml, uploadFile } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Bulk Import";

const STATUS_LABELS = {
  invalid: "Invalid",
  duplicate: "Duplicate",
  suppressed: "Suppressed",
};

export async function render(container) {
  container.innerHTML = `
    <h1>Bulk SMS Import</h1>
    <form id="upload-form" class="detail-panel">
      <div class="detail-grid">
        <label>Format
          <select name="format">
            <option value="phone_message">Phone number + message (each row has its own text)</option>
            <option value="phone_only">Phone number only (uses a shared template/message below)</option>
          </select>
        </label>
        <label>CSV file <input type="file" name="file" accept=".csv,text/csv" required /></label>
      </div>
      <p class="field-hint">
        <strong>phone_number,message</strong> format: <code>phone_number,message</code> header, one row per recipient
        with its own exact text.<br />
        <strong>phone_number only</strong> format: <code>phone_number</code> header, one column -- pick a template or
        type a message after uploading.
      </p>
      <div id="upload-error"></div>
      <div class="action-row">
        <button type="submit" class="btn" id="upload-btn">Upload &amp; validate</button>
      </div>
    </form>
    <div id="import-result"></div>
  `;

  const errorEl = container.querySelector("#upload-error");
  const resultEl = container.querySelector("#import-result");
  const uploadForm = container.querySelector("#upload-form");
  const uploadBtn = container.querySelector("#upload-btn");

  // One idempotency key per upload *attempt* -- regenerated only when the
  // operator picks a new file or changes format, not on every render, so
  // a double-click or a network retry of the *same* attempt reuses the
  // identical key and the server returns the already-computed summary
  // instead of re-parsing the file twice.
  let idempotencyKey = crypto.randomUUID();
  for (const el of uploadForm.querySelectorAll('input[type="file"], select[name="format"]')) {
    el.addEventListener("change", () => {
      idempotencyKey = crypto.randomUUID();
    });
  }

  uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorEl.innerHTML = "";
    const data = new FormData(uploadForm);
    const file = data.get("file");
    if (!file || file.size === 0) {
      toast("Choose a CSV file first", true);
      return;
    }
    const formData = new FormData();
    formData.set("file", file);
    formData.set("format", data.get("format"));
    formData.set("idempotency_key", idempotencyKey);

    uploadBtn.disabled = true;
    uploadBtn.textContent = "Uploading…";
    try {
      const summary = await uploadFile("/import/csv", formData);
      toast("CSV validated");
      renderSummary(summary);
    } catch (err) {
      renderError(errorEl, err);
    } finally {
      uploadBtn.disabled = false;
      uploadBtn.textContent = "Upload & validate";
    }
  });

  function renderSummary(summary) {
    const willSend = summary.valid_rows;
    resultEl.innerHTML = `
      <div class="detail-panel">
        <h2>CSV validation</h2>
        <div class="stat-grid">
          <div class="stat-card"><div class="stat-label">Total rows</div><div class="stat-value">${summary.total_rows.toLocaleString()}</div></div>
          <div class="stat-card"><div class="stat-label">Valid</div><div class="stat-value">${summary.valid_rows.toLocaleString()}</div></div>
          <div class="stat-card"><div class="stat-label">Invalid</div><div class="stat-value">${summary.invalid_rows.toLocaleString()}</div></div>
          <div class="stat-card"><div class="stat-label">Duplicates</div><div class="stat-value">${summary.duplicate_rows.toLocaleString()}</div></div>
          <div class="stat-card"><div class="stat-label">Suppressed</div><div class="stat-value">${summary.suppressed_rows.toLocaleString()}</div></div>
        </div>
        ${summary.unsupported_columns.length > 0
          ? `<p class="field-hint">Unrecognized column(s) in this file, ignored: ${summary.unsupported_columns.map(escapeHtml).join(", ")}</p>`
          : ""
        }
        ${willSend === 0
          ? `<p class="error-banner">No valid recipients -- nothing can be sent from this file. Fix the rows below and re-upload.</p>`
          : `<p class="field-hint"><strong>Will send: ${willSend.toLocaleString()}</strong> message(s) once this becomes a campaign and is started.</p>`
        }
        <div id="error-rows"></div>
        ${willSend > 0 ? createCampaignFormHtml(summary) : ""}
      </div>
    `;

    if (summary.invalid_rows + summary.duplicate_rows + summary.suppressed_rows > 0) {
      renderErrorRows(summary.job_id);
    }
    if (willSend > 0) {
      wireCreateCampaignForm(summary);
    }
  }

  async function renderErrorRows(jobId) {
    const errorRowsEl = resultEl.querySelector("#error-rows");
    errorRowsEl.innerHTML = `
      <div class="action-row">
        ${Object.entries(STATUS_LABELS).map(([status, text]) => `
          <button type="button" class="btn btn-secondary btn-sm" data-status="${status}">${text} rows</button>
        `).join("")}
      </div>
      <div id="error-rows-table"></div>
    `;
    const tableEl = errorRowsEl.querySelector("#error-rows-table");
    for (const btn of errorRowsEl.querySelectorAll("button[data-status]")) {
      btn.addEventListener("click", async () => {
        tableEl.innerHTML = `<p class="loading">Loading…</p>`;
        try {
          const rows = await api(`/import/${jobId}/rows?status=${btn.dataset.status}&limit=200`);
          tableEl.innerHTML = rows.length === 0
            ? `<p class="empty">No rows with this status.</p>`
            : `
              <table class="data-table">
                <thead><tr><th>Row</th><th>Phone (as entered)</th><th>Reason</th></tr></thead>
                <tbody>
                  ${rows.map((r) => `
                    <tr><td>${r.row_number}</td><td>${escapeHtml(r.raw_phone)}</td><td>${escapeHtml(r.error_reason || "")}</td></tr>
                  `).join("")}
                </tbody>
              </table>
              ${rows.length === 200 ? `<p class="field-hint">Showing the first 200 -- narrow down and re-upload if there are more.</p>` : ""}
            `;
        } catch (err) {
          renderError(tableEl, err);
        }
      });
    }
  }

  function createCampaignFormHtml(summary) {
    const needsMessage = summary.format === "phone_only";
    return `
      <h3>Create campaign from this import</h3>
      <form id="create-campaign-from-import-form" class="inline-form">
        <input type="text" name="name" placeholder="Campaign name" required />
        ${needsMessage
          ? `<input type="text" name="body_override" placeholder="Message text ({{display_name}} allowed)" required />`
          : `<p class="field-hint" style="margin:0">Each recipient's own message from the CSV will be sent as-is.</p>`
        }
        <input type="text" name="required_fleet_group" placeholder="Required node fleet (optional)" />
        <button type="submit" class="btn">Create draft campaign</button>
      </form>
      <div id="create-campaign-from-import-error"></div>
    `;
  }

  function wireCreateCampaignForm(summary) {
    const form = resultEl.querySelector("#create-campaign-from-import-form");
    const formErrorEl = resultEl.querySelector("#create-campaign-from-import-error");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      formErrorEl.innerHTML = "";
      const data = new FormData(form);
      try {
        const campaign = await api("/campaigns", {
          method: "POST",
          body: {
            name: data.get("name"),
            body_override: summary.format === "phone_only" ? data.get("body_override") : null,
            audience_filter: {},
            required_fleet_group: data.get("required_fleet_group") || null,
            import_job_id: summary.job_id,
          },
        });
        toast(`Draft campaign #${campaign.id} created -- open it from the Campaigns tab to validate and send`);
        form.reset();
        uploadForm.reset();
        idempotencyKey = crypto.randomUUID();
        resultEl.innerHTML = "";
      } catch (err) {
        renderError(formErrorEl, err);
      }
    });
  }
}
