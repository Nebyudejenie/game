import { escapeHtml } from "./api.js";

let toastTimer = null;

export function toast(message, isError = false) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = message;
  el.classList.toggle("toast-error", isError);
  el.classList.add("visible");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("visible"), 3500);
}

// The one shape every screen's own fetch-error catch renders: the real
// API error detail (e.g. "role 'support' lacks 'risk:view'"), not a
// generic message -- that's deliberate (see risk.js's own comment), so
// this only consolidates the repeated markup, not the underlying
// per-screen try/catch each screen still owns.
export function renderError(container, err) {
  container.innerHTML = `<p class="error-banner">${escapeHtml(err.detail || err.message)}</p>`;
}
