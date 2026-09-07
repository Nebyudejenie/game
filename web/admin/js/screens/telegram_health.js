import { api, escapeHtml, fmtDate } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Telegram";

const STATUS_EXPLAINER = {
  healthy: "Telegram is delivering updates normally.",
  warning: "A backlog is building up -- worth a look, not yet an incident.",
  critical: "A large backlog is stuck -- Telegram cannot reach the webhook, or it is failing to process updates.",
};

function fmtMs(ms) {
  return ms === null || ms === undefined ? "—" : `${ms.toFixed(1)}ms`;
}

function fmtPct(fraction) {
  return fraction === null || fraction === undefined ? "—" : `${(fraction * 100).toFixed(1)}%`;
}

export async function render(container) {
  container.innerHTML = `
    <h1>Telegram</h1>
    <p class="empty">
      A live check against Telegram's own getWebhookInfo -- made fresh every
      time this page loads, not cached. Time-series percentiles across a
      real traffic window live on the Grafana dashboard; the table below
      shows the same underlying counters and percentiles read directly
      from the bot's own /metrics right now.
    </p>
    <div id="webhook-health"><p class="loading">Loading…</p></div>

    <h2>Commands</h2>
    <p class="empty">
      Every real Telegram command handler in this codebase (services/bot/
      handlers.py) -- generated from live router introspection, see
      docs/BOT_COMMAND_CATALOG.md. Disabling a command here takes effect
      within ~30s (the bot's own registry poll interval) and shows every
      player a controlled, translated message instead of running the
      handler -- never a crash, never silence.
    </p>
    <div id="commands-list"><p class="loading">Loading…</p></div>
  `;

  const healthEl = container.querySelector("#webhook-health");
  const commandsEl = container.querySelector("#commands-list");

  await Promise.all([loadHealth(healthEl), loadCommands(commandsEl)]);
}

async function loadHealth(el) {
  try {
    const h = await api("/telegram/webhook-health");
    el.innerHTML = `
      <div class="stat-grid">
        <div class="stat-card${h.status !== "healthy" ? " stat-card-alert" : ""}">
          <div class="stat-label">Status</div>
          <div class="stat-value"><span class="badge badge-${h.status}">${h.status}</span></div>
        </div>
        <div class="stat-card${h.pending_update_count >= h.warning_threshold ? " stat-card-alert" : ""}">
          <div class="stat-label">Pending updates</div>
          <div class="stat-value">${h.pending_update_count}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Webhook URL</div>
          <div class="stat-value" style="font-size:0.85rem;word-break:break-all">${escapeHtml(h.url || "(not set)")}</div>
        </div>
      </div>

      <div class="detail-panel">
        <p>${STATUS_EXPLAINER[h.status] || ""}</p>
        <div class="detail-grid">
          <div><div class="field-label">Last error</div><div class="field-value">${h.last_error_message ? escapeHtml(h.last_error_message) : "None"}</div></div>
          <div><div class="field-label">Last error at</div><div class="field-value">${h.last_error_date ? fmtDate(h.last_error_date) : "—"}</div></div>
          <div><div class="field-label">Last sync error at</div><div class="field-value">${h.last_synchronization_error_date ? fmtDate(h.last_synchronization_error_date) : "—"}</div></div>
          <div><div class="field-label">IP address</div><div class="field-value">${h.ip_address ? escapeHtml(h.ip_address) : "—"}</div></div>
          <div><div class="field-label">Max connections</div><div class="field-value">${h.max_connections ?? "—"}</div></div>
        </div>
        <p class="empty" style="margin-top:0.75rem">
          Telegram's own API does not report how long the oldest pending
          update has been waiting -- only a raw count. Pending updates
          climbing over time, alongside a falling
          <code>telegram_commands_total</code> rate on Grafana, is the real
          signal of a stuck backlog rather than a brief, self-clearing spike.
        </p>
      </div>
    `;
  } catch (err) {
    renderError(el, err);
  }
}

async function loadCommands(el) {
  try {
    const commands = await api("/telegram/commands");
    renderCommands(el, commands);
  } catch (err) {
    renderError(el, err);
  }
}

function sortBySlowestFirst(commands) {
  // Section 7: "sort by slowest P95 so operators immediately see what
  // needs optimization" -- commands with real traffic (a real P95) sort
  // above every NO-DATA command, slowest first; NO-DATA commands keep
  // the registry's own sort_order among themselves.
  const withData = commands.filter((c) => c.metrics && c.metrics.p95_ms !== null);
  const withoutData = commands.filter((c) => !c.metrics || c.metrics.p95_ms === null);
  withData.sort((a, b) => b.metrics.p95_ms - a.metrics.p95_ms);
  return [...withData, ...withoutData];
}

function renderCommands(el, commandsUnsorted) {
  const commands = sortBySlowestFirst(commandsUnsorted);
  el.innerHTML = `
    <p class="empty">Sorted slowest (P95) first -- commands with no real traffic yet sort last.</p>
    <table class="data-table">
      <thead>
        <tr>
          <th>Command</th><th>Category</th><th>Enabled</th><th>Usage</th>
          <th>Success</th><th>P50</th><th>P95</th><th>P99</th><th>Disabled hits</th><th>Rate-limited</th><th></th>
        </tr>
      </thead>
      <tbody>
        ${commands.map((c) => `
          <tr data-handler="${c.handler_name}" class="clickable-row">
            <td>
              <strong>${c.command ? "/" + escapeHtml(c.command) : escapeHtml(c.display_name)}</strong>
              <div class="empty" style="margin:0">${escapeHtml(c.description)}</div>
            </td>
            <td>${escapeHtml(c.category)}</td>
            <td>
              <span class="badge badge-${c.enabled ? "active" : "banned"}">${c.enabled ? "enabled" : "disabled"}</span>
              ${!c.admin_managed ? '<div class="empty" style="margin:0">structural</div>' : ""}
            </td>
            <td>${c.metrics ? c.metrics.count : "NO DATA"}</td>
            <td>${c.metrics ? fmtPct(c.metrics.success_rate) : "NO DATA"}</td>
            <td>${c.metrics ? fmtMs(c.metrics.p50_ms) : "—"}</td>
            <td>${c.metrics ? fmtMs(c.metrics.p95_ms) : "—"}</td>
            <td>${c.metrics ? fmtMs(c.metrics.p99_ms) : "—"}</td>
            <td>${c.metrics ? c.metrics.blocked : "—"}</td>
            <td>${c.metrics ? c.metrics.rate_limited : "—"}</td>
            <td><button class="btn btn-secondary btn-sm details-btn">Details</button></td>
          </tr>
          <tr class="detail-row" data-detail-for="${c.handler_name}" hidden><td colspan="11"></td></tr>
        `).join("")}
      </tbody>
    </table>
  `;

  for (const row of el.querySelectorAll("tr[data-handler]")) {
    const handlerName = row.dataset.handler;
    const command = commands.find((c) => c.handler_name === handlerName);
    row.querySelector(".details-btn").addEventListener("click", () => toggleDetails(el, command));
  }
}

function toggleDetails(el, command) {
  const detailRow = el.querySelector(`tr[data-detail-for="${command.handler_name}"]`);
  const cell = detailRow.querySelector("td");
  if (!detailRow.hidden) {
    detailRow.hidden = true;
    return;
  }
  // Collapse any other open row first -- one detail panel open at a time
  // keeps this table readable with 18 real rows in it.
  for (const other of el.querySelectorAll(".detail-row")) {
    if (other !== detailRow) other.hidden = true;
  }

  cell.innerHTML = `
    <div class="detail-panel">
      <div class="detail-grid">
        <div><div class="field-label">Handler</div><div class="field-value">${escapeHtml(command.handler_name)}</div></div>
        <div><div class="field-label">Analytics key</div><div class="field-value">${command.analytics_key ? escapeHtml(command.analytics_key) : "(uses handler name)"}</div></div>
        <div><div class="field-label">Content key</div><div class="field-value">${command.content_key ? escapeHtml(command.content_key) : "—"}</div></div>
        <div><div class="field-label">Last changed</div><div class="field-value">${fmtDate(command.updated_at)}</div></div>
      </div>
      ${command.metrics ? `
        <div class="detail-grid" style="margin-top:0.5rem">
          <div><div class="field-label">Requests</div><div class="field-value">${command.metrics.count}</div></div>
          <div><div class="field-label">Errors</div><div class="field-value">${command.metrics.count > 0 ? Math.round(command.metrics.error_rate * command.metrics.count) : 0}</div></div>
          <div><div class="field-label">Disabled hits</div><div class="field-value">${command.metrics.blocked}</div></div>
          <div><div class="field-label">Rate-limited hits</div><div class="field-value">${command.metrics.rate_limited}</div></div>
        </div>
      ` : `<p class="empty" style="margin-top:0.5rem">NO DATA -- no real traffic for this command yet (or bot_metrics_url isn't configured in this environment).</p>`}

      <form class="edit-form">
        <div class="detail-grid">
          <label>Description <input type="text" name="description" value="${escapeHtml(command.description)}" /></label>
          <label>Category <input type="text" name="category" value="${escapeHtml(command.category)}" /></label>
          <label>Sort order <input type="number" name="sort_order" value="${command.sort_order}" /></label>
          <label>Cooldown (seconds, 0 = none)
            <input type="number" name="cooldown_seconds" value="${command.cooldown_seconds}" min="0" max="3600" />
          </label>
          <label>Rate limit (per minute, blank = unlimited)
            <input type="number" name="rate_limit_per_minute" value="${command.rate_limit_per_minute ?? ""}" min="1" max="1000" placeholder="unlimited" />
          </label>
        </div>
        <div class="action-row">
          ${
            command.admin_managed
              ? `<button type="button" class="btn btn-secondary toggle-enabled-btn">${command.enabled ? "Disable" : "Enable"}</button>`
              : `<span class="empty">A structural command -- cannot be disabled from here.</span>`
          }
          <button type="submit" class="btn">Save changes</button>
        </div>
        <p class="empty" style="margin-top:0.5rem">
          A configured limit takes effect within ~30s (the bot's own registry poll
          interval). A player who hits it sees "try again in N seconds" whenever that's
          computable -- never a bare "too many requests" or a Redis-level detail.
        </p>
      </form>

      ${command.content_key ? `
        <h3>Preview</h3>
        <div class="action-row">
          <label>Language
            <select class="preview-language">
              <option value="am">Amharic</option>
              <option value="en">English</option>
            </select>
          </label>
          <button type="button" class="btn btn-secondary preview-btn">Load preview</button>
        </div>
        <div class="preview-result"></div>

        <h3>Send test</h3>
        <form class="send-test-form">
          <div class="action-row">
            <label>Target Telegram user id <input type="number" name="target_telegram_id" required /></label>
            <label>Language
              <select name="language">
                <option value="am">Amharic</option>
                <option value="en">English</option>
              </select>
            </label>
            <button type="submit" class="btn">Send test</button>
          </div>
        </form>
        <p class="empty">
          Sends only to the exact Telegram user id entered above, never to
          any group of players -- there is no "broadcast" path from here.
        </p>
      ` : `<p class="empty">No content_key configured -- nothing to preview or test-send for this command.</p>`}
    </div>
  `;
  detailRow.hidden = false;

  const editForm = cell.querySelector(".edit-form");
  editForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(editForm);
    const rawRateLimit = data.get("rate_limit_per_minute");
    try {
      await api(`/telegram/commands/${command.handler_name}`, {
        method: "PATCH",
        body: {
          changes: {
            description: data.get("description"),
            category: data.get("category"),
            sort_order: Number(data.get("sort_order")),
            cooldown_seconds: Number(data.get("cooldown_seconds")),
            rate_limit_per_minute: rawRateLimit === "" ? null : Number(rawRateLimit),
          },
          reason: "Edited from the Telegram Commands screen",
        },
      });
      toast("Command updated.");
      await loadCommands(el);
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  });

  const toggleBtn = cell.querySelector(".toggle-enabled-btn");
  if (toggleBtn) {
    toggleBtn.addEventListener("click", async () => {
      const reason = window.prompt(
        `${command.enabled ? "Disabling" : "Enabling"} /${command.command || command.handler_name} -- reason for the audit log:`
      );
      if (reason === null) return;
      try {
        await api(`/telegram/commands/${command.handler_name}`, {
          method: "PATCH",
          body: { changes: { enabled: !command.enabled }, reason },
        });
        toast(`Command ${command.enabled ? "disabled" : "enabled"}.`);
        await loadCommands(el);
      } catch (err) {
        toast(err.detail || err.message, true);
      }
    });
  }

  const previewBtn = cell.querySelector(".preview-btn");
  if (previewBtn) {
    const previewResult = cell.querySelector(".preview-result");
    previewBtn.addEventListener("click", async () => {
      const language = cell.querySelector(".preview-language").value;
      previewResult.innerHTML = `<p class="loading">Loading…</p>`;
      try {
        const preview = await api(
          `/telegram/commands/${command.handler_name}/preview?language=${language}`
        );
        previewResult.innerHTML = `
          <div class="detail-panel">
            <div class="field-label">What the player sees</div>
            <div class="field-value">${escapeHtml(preview.rendered_preview)}</div>
            ${preview.placeholders.length > 0 ? `<p class="empty">Placeholders shown as [name] here are filled with real values (e.g. a real balance) when actually sent.</p>` : ""}
          </div>
        `;
      } catch (err) {
        renderError(previewResult, err);
      }
    });
  }

  const sendTestForm = cell.querySelector(".send-test-form");
  if (sendTestForm) {
    sendTestForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(sendTestForm);
      const targetTelegramId = Number(data.get("target_telegram_id"));
      if (!Number.isInteger(targetTelegramId) || targetTelegramId <= 0) {
        toast("Please enter a real Telegram user id.", true);
        return;
      }
      try {
        await api(`/telegram/commands/${command.handler_name}/send-test`, {
          method: "POST",
          body: { target_telegram_id: targetTelegramId, language: data.get("language") },
        });
        toast("Test message enqueued for delivery.");
      } catch (err) {
        toast(err.detail || err.message, true);
      }
    });
  }
}
