import { api, escapeHtml, fmtDate } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Ingestion Devices";

const HEALTH_BADGE_CLASS = {
  healthy: "healthy",
  degraded: "warning",
  awaiting_first_ingestion: "draft",
  revoked: "revoked",
};

const HEALTH_EXPLAINER = {
  healthy: "Ingesting normally.",
  degraded: "Registered and active, but no successful ingestion recently -- check the phone " +
    "(power, signal, MacroDroid still armed). The Telegram payment-agent fallback remains available " +
    "while this is investigated.",
  awaiting_first_ingestion: "Registered, but has never successfully ingested an SMS yet -- " +
    "finish the MacroDroid setup on the phone and send a test message.",
  revoked: "Credential revoked -- this device can no longer submit evidence.",
};

export async function render(container) {
  container.innerHTML = `
    <h1>Telebirr ingestion devices</h1>
    <p class="wallet-note">
      Per-device credentials for the automated Android/MacroDroid Telebirr ingestion path
      (services/payments/device_registry.py). This is the <strong>primary</strong> ingestion
      path once a device is set up; the Telegram payment-agent screen remains a working
      fallback if a device goes down. Every device still converges on the exact same
      evidence pipeline and recipient-matching rules as every other ingestion source --
      revoking or adding a device changes nothing about how a submitted SMS is judged.
    </p>
    <div id="devices-list"><p class="loading">Loading…</p></div>

    <h2>Register a device</h2>
    <p class="wallet-note">
      Pick a stable device_id (e.g. <code>samsung-a15-shop-till</code>) and a human-readable
      name. The token shown after creation is the phone's MacroDroid credential -- copy it
      into the macro's Authorization header immediately; it cannot be viewed again afterward,
      only rotated.
    </p>
    <form id="create-device-form" class="detail-panel">
      <div class="detail-grid">
        <label>Device id <input type="text" name="device_id" placeholder="samsung-a15-shop-till" required /></label>
        <label>Device name <input type="text" name="device_name" placeholder="Shop till Android #1" required /></label>
      </div>
      <div class="action-row">
        <button type="submit" class="btn">Register device</button>
      </div>
    </form>
    <div id="new-token-panel"></div>
  `;

  const listEl = container.querySelector("#devices-list");
  const createForm = container.querySelector("#create-device-form");
  const newTokenPanel = container.querySelector("#new-token-panel");

  async function reload() {
    listEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const devices = await api("/ingestion-devices");
      renderList(devices);
    } catch (err) {
      renderError(listEl, err);
    }
  }

  function renderList(devices) {
    if (devices.length === 0) {
      listEl.innerHTML = `<p class="empty">No ingestion devices registered yet -- the Telegram payment-agent path is the only active ingestion source until one is added here.</p>`;
      return;
    }
    listEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>Device</th><th>Health</th><th>Last seen</th><th>Last success</th>
            <th>Success</th><th>Duplicate</th><th>Failure</th><th>Auth failures</th><th></th>
          </tr>
        </thead>
        <tbody>
          ${devices.map((d) => `
            <tr data-device-pk="${d.id}" data-status="${d.status}">
              <td>
                <strong>${escapeHtml(d.device_name)}</strong>
                <div class="empty" style="margin:0">${escapeHtml(d.device_id)}</div>
              </td>
              <td>
                <span class="badge badge-${HEALTH_BADGE_CLASS[d.health] || "draft"}">${escapeHtml(d.health)}</span>
              </td>
              <td>${fmtDate(d.last_seen_at)}</td>
              <td>${fmtDate(d.last_success_at)}</td>
              <td>${d.success_count}</td>
              <td>${d.duplicate_count}</td>
              <td>${d.failure_count}${d.last_error_reason ? `<div class="empty" style="margin:0">${escapeHtml(d.last_error_reason)}</div>` : ""}</td>
              <td>${d.auth_failure_count}</td>
              <td>
                <button class="btn btn-secondary btn-sm rotate-token-btn">Rotate token</button>
                <button class="btn btn-secondary btn-sm toggle-status-btn">${d.status === "revoked" ? "Reactivate" : "Revoke"}</button>
              </td>
            </tr>
            <tr class="detail-row" data-health-note-for="${d.id}">
              <td colspan="9"><p class="empty" style="margin:0">${HEALTH_EXPLAINER[d.health] || ""}</p></td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
    for (const row of listEl.querySelectorAll("tr[data-device-pk]")) {
      const devicePk = Number(row.dataset.devicePk);
      const status = row.dataset.status;
      row.querySelector(".toggle-status-btn").addEventListener("click", () => toggleStatus(devicePk, status));
      row.querySelector(".rotate-token-btn").addEventListener("click", () => rotateToken(devicePk));
    }
  }

  function showToken(heading, result) {
    newTokenPanel.innerHTML = `
      <div class="detail-panel">
        <p class="form-error">
          ${heading} -- shown once, never retrievable again. Copy this into the phone's
          MacroDroid HTTP Request action as: <code>Authorization: Bearer &lt;token&gt;</code>
        </p>
        <p class="wallet-note">Device id: <code>${escapeHtml(result.device_id)}</code></p>
        <pre class="code-block">${escapeHtml(result.token)}</pre>
      </div>
    `;
  }

  async function toggleStatus(devicePk, currentStatus) {
    const nextStatus = currentStatus === "revoked" ? "active" : "revoked";
    if (nextStatus === "revoked" && !window.confirm(
      "Revoke this device's credential? It will immediately stop being able to submit evidence " +
      "(the Telegram payment-agent path remains available as a fallback)."
    )) {
      return;
    }
    try {
      await api(`/ingestion-devices/${devicePk}`, { method: "PATCH", body: { status: nextStatus } });
      toast(nextStatus === "revoked" ? "Device revoked." : "Device reactivated.");
      reload();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  async function rotateToken(devicePk) {
    if (!window.confirm("Rotate this device's token? The old token stops working immediately -- " +
      "the phone's MacroDroid configuration must be updated with the new one right away.")) {
      return;
    }
    try {
      const result = await api(`/ingestion-devices/${devicePk}/rotate-token`, { method: "POST" });
      showToken("New device token", result);
      toast("Token rotated -- update the phone now.");
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  createForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    newTokenPanel.innerHTML = "";
    const data = new FormData(createForm);
    try {
      const result = await api("/ingestion-devices", {
        method: "POST",
        body: { device_id: data.get("device_id"), device_name: data.get("device_name") },
      });
      toast("Device registered.");
      createForm.reset();
      showToken("Device token", result);
      reload();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  });

  await reload();
}
