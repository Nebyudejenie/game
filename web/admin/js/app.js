import { getToken, clearToken, api, ApiError } from "./api.js";
import * as loginScreen from "./screens/login.js";
import * as dashboardScreen from "./screens/dashboard.js";
import * as usersScreen from "./screens/users.js";
import * as paymentsScreen from "./screens/payments.js";
import * as roundsScreen from "./screens/rounds.js";
import * as roomsScreen from "./screens/rooms.js";
import * as reportsScreen from "./screens/reports.js";
import * as riskScreen from "./screens/risk.js";
import * as auditScreen from "./screens/audit.js";

// Order here is the nav order. Each screen owns its own error handling
// (an inline banner using the real API error detail, e.g. "role 'support'
// lacks 'payments:view'") -- the try/catch below is only a safety net for
// a screen module throwing somewhere it didn't already handle.
const SCREENS = {
  dashboard: dashboardScreen,
  users: usersScreen,
  payments: paymentsScreen,
  rounds: roundsScreen,
  rooms: roomsScreen,
  reports: reportsScreen,
  risk: riskScreen,
  audit: auditScreen,
};

const loginEl = document.getElementById("login-screen");
const shellEl = document.getElementById("app-shell");
const navEl = document.getElementById("nav");
const contentEl = document.getElementById("content");

function buildNav(active) {
  navEl.innerHTML = `
    <div class="nav-brand">Jo Bingo Admin</div>
    ${Object.entries(SCREENS).map(([name, mod]) => `
      <button class="nav-btn ${name === active ? "active" : ""}" data-screen="${name}">${mod.label}</button>
    `).join("")}
    <button class="nav-btn nav-logout" id="logout-btn">Log out</button>
  `;
  for (const btn of navEl.querySelectorAll(".nav-btn[data-screen]")) {
    btn.addEventListener("click", () => showScreen(btn.dataset.screen));
  }
  navEl.querySelector("#logout-btn").addEventListener("click", doLogout);
}

async function showScreen(name) {
  buildNav(name);
  contentEl.innerHTML = `<p class="loading">Loading…</p>`;
  try {
    await SCREENS[name].render(contentEl);
  } catch (err) {
    if (err instanceof ApiError && err.status === 403) {
      contentEl.innerHTML = `<div class="error-banner">Your role does not have access to this screen.</div>`;
    } else if (err instanceof ApiError && err.status === 401) {
      // handled globally by the admin:unauthorized listener below
    } else {
      contentEl.innerHTML = `<div class="error-banner">Failed to load: ${err.message}</div>`;
    }
  }
}

async function doLogout() {
  try {
    await api("/auth/logout", { method: "POST" });
  } catch {
    // best-effort -- the token gets cleared client-side regardless
  }
  clearToken();
  showLogin();
}

function showApp() {
  loginEl.hidden = true;
  shellEl.hidden = false;
  showScreen("dashboard");
}

function showLogin() {
  shellEl.hidden = true;
  loginEl.hidden = false;
  loginScreen.render(loginEl, showApp);
}

window.addEventListener("admin:unauthorized", showLogin);

if (getToken()) {
  showApp();
} else {
  showLogin();
}
