import { api, escapeHtml, fmtDate } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Admin Users";

const ROLES = ["support", "finance", "ops", "superadmin"];

export async function render(container) {
  container.innerHTML = `
    <h1>Admin users</h1>
    <p class="wallet-note">
      Every account that can log into this console -- support, finance, ops, and superadmin. Creating,
      deactivating, or changing someone's role here is superadmin-only, the highest-leverage setting in
      the whole system.
    </p>
    <div id="admin-users-list"><p class="loading">Loading…</p></div>

    <h2>Add admin user</h2>
    <form id="create-admin-form" class="detail-panel">
      <div class="detail-grid">
        <label>Username <input type="text" name="username" required autocomplete="off" /></label>
        <label>Temporary password <input type="text" name="password" required minlength="12" /></label>
        <label>Role
          <select name="role">
            ${ROLES.map((r) => `<option value="${r}">${r}</option>`).join("")}
          </select>
        </label>
      </div>
      <div class="action-row">
        <button type="submit" class="btn">Create account</button>
      </div>
    </form>
    <div id="new-account-secret"></div>
  `;

  const listEl = container.querySelector("#admin-users-list");
  const createForm = container.querySelector("#create-admin-form");
  const secretEl = container.querySelector("#new-account-secret");

  async function reload() {
    listEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const admins = await api("/admin-users");
      renderList(admins);
    } catch (err) {
      renderError(listEl, err);
    }
  }

  function renderList(admins) {
    listEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr><th>Username</th><th>Role</th><th>Active</th><th>Created</th><th>Last login</th><th></th></tr>
        </thead>
        <tbody>
          ${admins.map((a) => `
            <tr data-admin-id="${a.id}">
              <td>${escapeHtml(a.username)}</td>
              <td>
                <select class="role-select">
                  ${ROLES.map((r) => `<option value="${r}" ${r === a.role ? "selected" : ""}>${r}</option>`).join("")}
                </select>
              </td>
              <td>${a.is_active ? "yes" : "no"}</td>
              <td>${fmtDate(a.created_at)}</td>
              <td>${fmtDate(a.last_login_at)}</td>
              <td>
                <button class="btn btn-secondary btn-sm toggle-active-btn">${a.is_active ? "Deactivate" : "Activate"}</button>
                <button class="btn btn-secondary btn-sm reset-password-btn">Reset password</button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
    for (const row of listEl.querySelectorAll("tr[data-admin-id]")) {
      const targetId = Number(row.dataset.adminId);
      const isActive = row.querySelector(".toggle-active-btn").textContent.trim() === "Deactivate";

      row.querySelector(".toggle-active-btn").addEventListener("click", async () => {
        if (!window.confirm(`${isActive ? "Deactivate" : "Activate"} this account? ${isActive ? "They will be logged out and unable to log back in." : ""}`)) return;
        try {
          await api(`/admin-users/${targetId}/active`, { method: "PATCH", body: { is_active: !isActive } });
          toast("Account updated.");
          reload();
        } catch (err) {
          toast(err.detail || err.message, true);
        }
      });

      row.querySelector(".role-select").addEventListener("change", async (event) => {
        const newRole = event.target.value;
        if (!window.confirm(`Change this account's role to "${newRole}"?`)) {
          reload();
          return;
        }
        try {
          await api(`/admin-users/${targetId}/role`, { method: "PATCH", body: { role: newRole } });
          toast("Role updated.");
          reload();
        } catch (err) {
          toast(err.detail || err.message, true);
          reload();
        }
      });

      row.querySelector(".reset-password-btn").addEventListener("click", async () => {
        const newPassword = window.prompt("New temporary password (at least 12 characters):");
        if (!newPassword) return;
        try {
          await api(`/admin-users/${targetId}/reset-password`, {
            method: "POST",
            body: { new_password: newPassword },
          });
          toast("Password reset. Tell them the new password through a secure channel.");
        } catch (err) {
          toast(err.detail || err.message, true);
        }
      });
    }
  }

  createForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(createForm);
    secretEl.innerHTML = "";
    try {
      const result = await api("/admin-users", {
        method: "POST",
        body: {
          username: data.get("username"),
          password: data.get("password"),
          role: data.get("role"),
        },
      });
      toast("Account created.");
      createForm.reset();
      secretEl.innerHTML = `
        <div class="detail-panel">
          <p class="form-error">
            TOTP secret -- shown once, never retrievable again. Scan this into an authenticator app now:
          </p>
          <pre class="code-block">${escapeHtml(result.totp_secret)}</pre>
          <p class="wallet-note">Provisioning URI: <code>${escapeHtml(result.totp_provisioning_uri)}</code></p>
        </div>
      `;
      reload();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  });

  await reload();
}
