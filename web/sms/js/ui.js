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

export function renderError(container, err) {
  container.innerHTML = `<p class="error-banner">${escapeHtml(err.detail || err.message)}</p>`;
}

export function badge(status) {
  return `<span class="badge badge-${escapeHtml(status)}">${escapeHtml(status)}</span>`;
}
