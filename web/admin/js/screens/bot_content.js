import { api, escapeHtml } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Bot Content";

const LANGUAGE_LABELS = { am: "Amharic", en: "English", om: "Oromo", ti: "Tigrinya" };

export async function render(container) {
  container.innerHTML = `
    <h1>Bot content</h1>
    <p class="wallet-note">
      Every piece of text the Telegram bot sends a player -- the main menu button labels, prompts,
      confirmations -- goes through here. Editing a value below changes what players see live, within
      about 30 seconds, with no code deploy. A value showing its shipped default (not yet customized) is
      shown lighter; "Reset to default" removes your override and goes back to it.
    </p>
    <form id="content-search-form" class="inline-form">
      <input type="text" id="content-search-input" placeholder="Search by key or category, e.g. menu" />
      <button type="submit" class="btn">Filter</button>
    </form>
    <div id="content-list"><p class="loading">Loading…</p></div>
  `;

  const searchForm = container.querySelector("#content-search-form");
  const searchInput = container.querySelector("#content-search-input");
  const listEl = container.querySelector("#content-list");

  let allItems = [];

  async function load() {
    listEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      allItems = await api("/bot-content");
      renderList();
    } catch (err) {
      renderError(listEl, err);
    }
  }

  function renderList() {
    const query = searchInput.value.trim().toLowerCase();
    const items = query
      ? allItems.filter((item) => item.key.toLowerCase().includes(query) || item.category.toLowerCase().includes(query))
      : allItems;

    if (items.length === 0) {
      listEl.innerHTML = `<p class="empty">No bot content keys match this search.</p>`;
      return;
    }

    const byCategory = new Map();
    for (const item of items) {
      if (!byCategory.has(item.category)) byCategory.set(item.category, []);
      byCategory.get(item.category).push(item);
    }

    listEl.innerHTML = [...byCategory.entries()].map(([category, categoryItems]) => `
      <h2>${escapeHtml(category)}</h2>
      <table class="data-table">
        <thead><tr><th>Key</th><th>Amharic (default language)</th><th>Customized?</th><th></th></tr></thead>
        <tbody>
          ${categoryItems.map((item) => `
            <tr data-key="${escapeHtml(item.key)}">
              <td><code>${escapeHtml(item.key)}</code>${item.placeholders.length ? `<div class="wallet-note">needs: ${item.placeholders.map((p) => `{${p}}`).join(", ")}</div>` : ""}</td>
              <td>${escapeHtml(truncate(item.languages.am.current_value || ""))}</td>
              <td>${Object.values(item.languages).some((l) => l.is_overridden) ? "yes" : "no"}</td>
              <td><button class="btn btn-secondary btn-sm edit-btn">Edit</button></td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `).join("");

    for (const row of listEl.querySelectorAll("tr[data-key]")) {
      row.querySelector(".edit-btn").addEventListener("click", () => openEditor(row.dataset.key));
    }
  }

  function truncate(text, max = 80) {
    return text.length > max ? `${text.slice(0, max)}…` : text;
  }

  function openEditor(key) {
    const item = allItems.find((i) => i.key === key);
    const existing = listEl.querySelector("#content-edit-panel");
    if (existing) existing.remove();

    const panel = document.createElement("div");
    panel.id = "content-edit-panel";
    panel.className = "detail-panel";
    panel.innerHTML = `
      <h2 style="margin-top:0"><code>${escapeHtml(item.key)}</code></h2>
      ${item.placeholders.length ? `<p class="wallet-note">Every language's text below must contain exactly these placeholders: ${item.placeholders.map((p) => `<code>{${p}}</code>`).join(", ")}</p>` : ""}
      ${Object.entries(item.languages).map(([lang, data]) => `
        <div class="detail-grid" style="grid-template-columns: 1fr;">
          <label>${LANGUAGE_LABELS[lang]} (${lang})${data.is_overridden ? " -- customized" : " -- shipped default"}
            <textarea rows="2" data-lang="${lang}">${escapeHtml(data.current_value || "")}</textarea>
          </label>
        </div>
        <div class="action-row">
          <button class="btn btn-secondary btn-sm save-btn" data-lang="${lang}">Save ${lang}</button>
          <button class="btn btn-secondary btn-sm reset-btn" data-lang="${lang}" ${data.is_overridden ? "" : "disabled"}>Reset ${lang} to default</button>
        </div>
      `).join("")}
      <div class="action-row">
        <button type="button" class="btn btn-secondary" id="close-editor-btn">Close</button>
      </div>
    `;
    listEl.prepend(panel);
    panel.scrollIntoView({ behavior: "smooth", block: "start" });

    panel.querySelector("#close-editor-btn").addEventListener("click", () => panel.remove());

    for (const btn of panel.querySelectorAll(".save-btn")) {
      btn.addEventListener("click", async () => {
        const lang = btn.dataset.lang;
        const value = panel.querySelector(`textarea[data-lang="${lang}"]`).value;
        try {
          await api(`/bot-content/${encodeURIComponent(key)}/${lang}`, { method: "PUT", body: { value } });
          toast(`Saved ${lang}. Live for players within about 30 seconds.`);
          await load();
          openEditor(key);
        } catch (err) {
          toast(err.detail || err.message, true);
        }
      });
    }

    for (const btn of panel.querySelectorAll(".reset-btn")) {
      btn.addEventListener("click", async () => {
        const lang = btn.dataset.lang;
        if (!window.confirm(`Reset ${lang} back to the shipped default?`)) return;
        try {
          await api(`/bot-content/${encodeURIComponent(key)}/${lang}`, { method: "DELETE" });
          toast(`Reset ${lang} to default.`);
          await load();
          openEditor(key);
        } catch (err) {
          toast(err.detail || err.message, true);
        }
      });
    }
  }

  searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    renderList();
  });

  await load();
}
