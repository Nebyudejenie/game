// Thin fetch wrapper around the admin API (services/admin/app.py), which
// this page is served from at /console -- every call below is a plain
// same-origin relative path, no base URL needed.

const TOKEN_KEY = "jobingo_admin_token";
// Stored purely so app.js can filter which nav items it shows -- a UX
// nicety, not a security boundary; every route re-checks the real role
// server-side via the bearer token regardless of what this holds.
const ROLE_KEY = "jobingo_admin_role";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export function getRole() {
  return localStorage.getItem(ROLE_KEY);
}

export function setRole(role) {
  localStorage.setItem(ROLE_KEY, role);
}

export function clearRole() {
  localStorage.removeItem(ROLE_KEY);
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

export async function api(path, { method = "GET", body } = {}) {
  const token = getToken();
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const response = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  // Only a *rejected* session token counts as "logged out" -- an
  // anonymous 401 (a plain wrong-password /auth/login attempt, which has
  // no token to send in the first place) is a normal, locally-handled
  // form error, not a session-expiry event the whole app should react to.
  if (response.status === 401 && token) {
    clearToken();
    clearRole();
    window.dispatchEvent(new CustomEvent("admin:unauthorized"));
  }

  if (!response.ok) {
    const detail = payload && typeof payload === "object" && "detail" in payload
      ? payload.detail
      : payload || `request failed (${response.status})`;
    throw new ApiError(response.status, detail);
  }
  return payload;
}

// Every jsonb column in this codebase comes back from asyncpg as a raw
// JSON string unless a codec is registered for it (see services/admin
// /queries.py's own json.loads() calls for the same reason) -- callers
// that render a jsonb field pass it through this first rather than
// assuming it's already an object.
export function parseMaybeJson(value) {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

export function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

export function fmtDate(value) {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString();
}
