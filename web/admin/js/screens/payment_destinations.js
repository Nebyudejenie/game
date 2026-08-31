import { api, escapeHtml } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Payment Destinations";

// Same vocabulary as payment_methods.kind and manual_payment_destinations
// .method_kind's own DB CHECK constraint (migrations/versions/
// 60dc29201d1c_manual_payments.py).
const METHOD_KINDS = ["telebirr", "cbe_birr", "cbe_account", "boa", "awash", "bank"];

export async function render(container) {
  container.innerHTML = `
    <h1>Manual deposit destinations</h1>
    <p class="wallet-note">
      The company accounts players are shown when they choose Manual Deposit. Only active
      destinations are shown to players.
    </p>
    <div id="destinations-list"><p class="loading">Loading…</p></div>

    <h2>Add destination</h2>
    <form id="create-destination-form" class="detail-panel">
      <div class="detail-grid">
        <label>Method
          <select name="method_kind">
            ${METHOD_KINDS.map((k) => `<option value="${k}">${k}</option>`).join("")}
          </select>
        </label>
        <label>Account / number <input type="text" name="account_ref" required /></label>
        <label>Account name <input type="text" name="account_name" required /></label>
        <label>Instructions <input type="text" name="instructions" placeholder="Optional, shown to the player" /></label>
      </div>
      <div class="action-row">
        <button type="submit" class="btn">Add destination</button>
      </div>
    </form>
  `;

  const listEl = container.querySelector("#destinations-list");
  const createForm = container.querySelector("#create-destination-form");

  let destinationsById = new Map();

  async function reload() {
    listEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const destinations = await api("/manual-payment-destinations");
      renderList(destinations);
    } catch (err) {
      renderError(listEl, err);
    }
  }

  function renderList(destinations) {
    destinationsById = new Map(destinations.map((d) => [d.id, d]));
    if (destinations.length === 0) {
      listEl.innerHTML = `<p class="empty">No manual deposit destinations configured yet.</p>`;
      return;
    }
    listEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr><th>Method</th><th>Account</th><th>Name</th><th>Instructions</th><th>Active</th><th></th></tr>
        </thead>
        <tbody>
          ${destinations.map((d) => `
            <tr data-destination-id="${d.id}">
              <td>${escapeHtml(d.method_kind)}</td>
              <td>${escapeHtml(d.account_ref)}</td>
              <td>${escapeHtml(d.account_name)}</td>
              <td>${escapeHtml(d.instructions || "—")}</td>
              <td>${d.is_active ? "yes" : "no"}</td>
              <td>
                <button class="btn btn-secondary btn-sm edit-destination-btn">Edit</button>
                <button class="btn btn-secondary btn-sm toggle-active-btn">${d.is_active ? "Deactivate" : "Activate"}</button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>
      <div id="destination-edit-panel"></div>
    `;
    for (const row of listEl.querySelectorAll("tr[data-destination-id]")) {
      const destinationId = Number(row.dataset.destinationId);
      const isActive = row.querySelector(".toggle-active-btn").textContent.trim() === "Deactivate";
      row.querySelector(".toggle-active-btn").addEventListener("click", () => toggleActive(destinationId, isActive));
      row.querySelector(".edit-destination-btn").addEventListener("click", () => openEditForm(destinationId));
    }
  }

  function openEditForm(destinationId) {
    const destination = destinationsById.get(destinationId);
    const panel = listEl.querySelector("#destination-edit-panel");
    panel.innerHTML = `
      <form id="edit-destination-form" class="detail-panel">
        <h2>Edit destination #${destinationId}</h2>
        <div class="detail-grid">
          <label>Account / number <input type="text" name="account_ref" value="${escapeHtml(destination.account_ref)}" required /></label>
          <label>Account name <input type="text" name="account_name" value="${escapeHtml(destination.account_name)}" required /></label>
          <label>Instructions <input type="text" name="instructions" value="${escapeHtml(destination.instructions || "")}" /></label>
        </div>
        <div class="action-row">
          <button type="submit" class="btn">Save changes</button>
          <button type="button" class="btn btn-secondary" id="cancel-edit-btn">Cancel</button>
        </div>
      </form>
    `;
    panel.querySelector("#cancel-edit-btn").addEventListener("click", () => {
      panel.innerHTML = "";
    });
    panel.querySelector("#edit-destination-form").addEventListener("submit", (event) => {
      event.preventDefault();
      saveEdit(destinationId, destination, event.target);
    });
  }

  async function saveEdit(destinationId, destination, form) {
    const data = new FormData(form);
    const candidate = {
      account_ref: data.get("account_ref"),
      account_name: data.get("account_name"),
      instructions: data.get("instructions") || null,
    };
    const changes = {};
    for (const [field, value] of Object.entries(candidate)) {
      if (String(destination[field] ?? "") !== String(value ?? "")) changes[field] = value;
    }
    if (Object.keys(changes).length === 0) {
      toast("Nothing changed.");
      return;
    }
    const reason = window.prompt(`Reason for editing destination #${destinationId}:`);
    if (reason === null) return;
    try {
      await api(`/manual-payment-destinations/${destinationId}`, {
        method: "PATCH",
        body: { changes, reason: reason || null },
      });
      toast("Destination updated.");
      reload();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  async function toggleActive(destinationId, currentlyActive) {
    const reason = window.prompt(`Reason to ${currentlyActive ? "deactivate" : "activate"} this destination:`);
    if (reason === null) return;
    try {
      await api(`/manual-payment-destinations/${destinationId}`, {
        method: "PATCH",
        body: { changes: { is_active: !currentlyActive }, reason: reason || null },
      });
      toast("Destination updated.");
      reload();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  createForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(createForm);
    try {
      await api("/manual-payment-destinations", {
        method: "POST",
        body: {
          method_kind: data.get("method_kind"),
          account_ref: data.get("account_ref"),
          account_name: data.get("account_name"),
          instructions: data.get("instructions") || null,
        },
      });
      toast("Destination added.");
      createForm.reset();
      reload();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  });

  await reload();
}
