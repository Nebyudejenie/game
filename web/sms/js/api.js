// Thin fetch wrapper around the SMS Control Plane API (services/sms/app.py),
// which this page is served from at /console -- every call below is a
// plain same-origin relative path. Mirrors web/admin/js/api.js's own
// shape exactly (a genuinely separate frontend, own localStorage keys so
// the two consoles' sessions never collide in a browser with both open).

const TOKEN_KEY = "jobingo_sms_token";
const ROLE_KEY = "jobingo_sms_role";

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

  if (response.status === 401 && token) {
    clearToken();
    clearRole();
    window.dispatchEvent(new CustomEvent("sms:unauthorized"));
  }

  if (!response.ok) {
    const detail = payload && typeof payload === "object" && "detail" in payload
      ? payload.detail
      : payload || `request failed (${response.status})`;
    throw new ApiError(response.status, detail);
  }
  return payload;
}

// A separate path from api() above, not a mode of it -- multipart/form
// -data (a real file, not a JSON body) needs the browser to set its own
// Content-Type (with the multipart boundary), which api()'s hardcoded
// "application/json" header would otherwise silently break. Mirrors
// api()'s own auth-header/401-handling exactly, so an expired session
// during a CSV upload behaves identically to every other request.
export async function uploadFile(path, formData) {
  const token = getToken();
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;

  const response = await fetch(path, { method: "POST", headers, body: formData });

  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  if (response.status === 401 && token) {
    clearToken();
    clearRole();
    window.dispatchEvent(new CustomEvent("sms:unauthorized"));
  }

  if (!response.ok) {
    const detail = payload && typeof payload === "object" && "detail" in payload
      ? payload.detail
      : payload || `request failed (${response.status})`;
    throw new ApiError(response.status, detail);
  }
  return payload;
}

// Every jsonb column comes back from asyncpg as a raw JSON string unless
// a codec is registered for it -- callers that render a jsonb field pass
// it through this first rather than assuming it's already an object
// (mirrors web/admin/js/api.js's own parseMaybeJson exactly).
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
