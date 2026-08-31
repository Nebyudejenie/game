import { api, escapeHtml } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Provider Availability";

const PROVIDERS = ["chapa", "santimpay", "arifpay", "manual"];
const DIRECTIONS = ["in", "out"];
const DIRECTION_LABEL = { in: "Deposits", out: "Withdrawals" };

export async function render(container) {
  container.innerHTML = `
    <h1>Payment provider availability</h1>
    <p class="wallet-note">
      Which rails players actually see. The Mini App and the bot both read this live --
      nothing here is hardcoded on the player-facing side.
    </p>
    <div id="availability-grid"><p class="loading">Loading…</p></div>
  `;
  const gridEl = container.querySelector("#availability-grid");

  async function reload() {
    gridEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const rows = await api("/payment-provider-availability");
      renderGrid(rows);
    } catch (err) {
      renderError(gridEl, err);
    }
  }

  function renderGrid(rows) {
    const byKey = new Map(rows.map((r) => [`${r.provider}:${r.direction}`, r]));
    gridEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr><th>Provider</th>${DIRECTIONS.map((d) => `<th>${DIRECTION_LABEL[d]}</th>`).join("")}</tr>
        </thead>
        <tbody>
          ${PROVIDERS.map((provider) => `
            <tr>
              <td>${escapeHtml(provider)}</td>
              ${DIRECTIONS.map((direction) => {
                const row = byKey.get(`${provider}:${direction}`);
                return `
                  <td>
                    <label style="flex-direction:row; align-items:center; gap:0.35rem;">
                      <input type="checkbox" data-provider="${provider}" data-direction="${direction}"
                        ${row && row.enabled ? "checked" : ""} />
                      enabled
                    </label>
                  </td>
                `;
              }).join("")}
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
    for (const checkbox of gridEl.querySelectorAll("input[type=checkbox]")) {
      checkbox.addEventListener("change", () => toggle(checkbox));
    }
  }

  async function toggle(checkbox) {
    const { provider, direction } = checkbox.dataset;
    const enabled = checkbox.checked;
    const reason = window.prompt(
      `Reason to ${enabled ? "enable" : "disable"} ${provider} for ${DIRECTION_LABEL[direction].toLowerCase()}:`
    );
    if (reason === null) {
      checkbox.checked = !enabled; // revert -- the admin cancelled
      return;
    }
    if (!reason.trim()) {
      toast("A reason is required.", true);
      checkbox.checked = !enabled;
      return;
    }
    try {
      await api(`/payment-provider-availability/${provider}/${direction}`, {
        method: "PATCH",
        body: { enabled, reason },
      });
      toast(`${provider} ${DIRECTION_LABEL[direction].toLowerCase()} ${enabled ? "enabled" : "disabled"}.`);
    } catch (err) {
      checkbox.checked = !enabled;
      toast(err.detail || err.message, true);
    }
  }

  await reload();
}
