import { api, escapeHtml } from "./api.js";

const CATEGORY_LABELS = {
  users: "Users",
  rooms: "Rooms",
  rounds: "Rounds",
  payments: "Payments",
  bonuses: "Bonuses",
  commands: "Telegram Commands",
  notifications: "Notifications",
  audit: "Audit Log",
};

// Debounced -- one request per pause in typing, not one per keystroke,
// the same discipline any search-as-you-type UI needs regardless of how
// fast the backend query itself is.
const DEBOUNCE_MS = 200;

let debounceTimer = null;
let lastRequestId = 0;

export function initGlobalSearch(showScreen) {
  const overlay = document.getElementById("global-search-overlay");
  const input = document.getElementById("global-search-input");
  const resultsEl = document.getElementById("global-search-results");

  function open() {
    overlay.hidden = false;
    input.value = "";
    resultsEl.innerHTML = `<p class="empty" style="padding:1rem">Type at least 2 characters…</p>`;
    input.focus();
  }

  function close() {
    overlay.hidden = true;
  }

  document.addEventListener("keydown", (event) => {
    const isCmdK = (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k";
    if (isCmdK) {
      event.preventDefault();
      if (overlay.hidden) open();
      else close();
      return;
    }
    if (!overlay.hidden && event.key === "Escape") {
      close();
    }
  });

  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });

  input.addEventListener("input", () => {
    clearTimeout(debounceTimer);
    const query = input.value.trim();
    if (query.length < 2) {
      resultsEl.innerHTML = `<p class="empty" style="padding:1rem">Type at least 2 characters…</p>`;
      return;
    }
    debounceTimer = setTimeout(() => runSearch(query), DEBOUNCE_MS);
  });

  async function runSearch(query) {
    const requestId = ++lastRequestId;
    resultsEl.innerHTML = `<p class="loading" style="padding:1rem">Searching…</p>`;
    let results;
    try {
      results = await api(`/search?q=${encodeURIComponent(query)}`);
    } catch (err) {
      if (requestId !== lastRequestId) return; // a newer keystroke already superseded this request
      resultsEl.innerHTML = `<p class="empty" style="padding:1rem">${escapeHtml(err.detail || err.message || "Search failed")}</p>`;
      return;
    }
    if (requestId !== lastRequestId) return; // stale response for an earlier keystroke -- discard

    const categories = Object.keys(results);
    if (categories.length === 0) {
      resultsEl.innerHTML = `<p class="empty" style="padding:1rem">No matches.</p>`;
      return;
    }

    resultsEl.innerHTML = categories.map((category) => `
      <div class="search-category-label">${CATEGORY_LABELS[category] || escapeHtml(category)}</div>
      ${results[category].map((r) => `
        <div class="search-result-row" data-screen="${escapeHtml(r.screen)}">
          <div class="field-value">${escapeHtml(r.label)}</div>
          <div class="empty" style="margin:0">${escapeHtml(r.subtitle)}</div>
        </div>
      `).join("")}
    `).join("");

    for (const row of resultsEl.querySelectorAll(".search-result-row")) {
      row.addEventListener("click", () => {
        close();
        showScreen(row.dataset.screen);
      });
    }
  }
}
