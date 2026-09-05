// Payment Agent Portal -- reads a one-time login token from the URL
// (sent by the bot's /portal command), exchanges it for a session, then
// shows only that agent's own submission history. No password anywhere:
// see services/payments/agent_auth.py's own docstring for why.

const SESSION_KEY = "agent_portal_session";

const loginScreen = document.getElementById("login-screen");
const loginStatus = document.getElementById("login-status");
const appShell = document.getElementById("app-shell");
const agentNameEl = document.getElementById("agent-name");
const submissionsRegion = document.getElementById("submissions-region");
const toast = document.getElementById("toast");

function showToast(text) {
  toast.textContent = text;
  toast.classList.add("visible");
  setTimeout(() => toast.classList.remove("visible"), 2500);
}

function getSession() {
  return localStorage.getItem(SESSION_KEY);
}

function setSession(token) {
  localStorage.setItem(SESSION_KEY, token);
}

function clearSession() {
  localStorage.removeItem(SESSION_KEY);
}

async function api(path, options = {}) {
  const session = getSession();
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (session) headers.Authorization = `Bearer ${session}`;
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    clearSession();
    throw new ApiError("session expired", 401);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(body.detail || `request failed (${response.status})`, response.status);
  }
  return response.json();
}

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function badgeClass(status) {
  return `badge badge-${status}`;
}

function formatAmount(amount) {
  return amount == null ? "—" : `${amount.toFixed(2)} ETB`;
}

function formatTimestamp(iso) {
  return new Date(iso).toLocaleString();
}

function renderSubmissions(rows) {
  if (rows.length === 0) {
    submissionsRegion.innerHTML = `<div class="state-message">No submissions yet -- forward a Telebirr SMS to the bot to see it appear here.</div>`;
    return;
  }
  const body = rows
    .map(
      (row) => `
      <tr>
        <td>${row.reference}</td>
        <td>${formatAmount(row.amount)}</td>
        <td><span class="${badgeClass(row.status)}">${row.status}</span></td>
        <td>${row.reject_reason || "—"}</td>
        <td>${formatTimestamp(row.received_at)}</td>
      </tr>`
    )
    .join("");
  submissionsRegion.innerHTML = `
    <table>
      <thead>
        <tr><th>Reference</th><th>Amount</th><th>Status</th><th>Reason</th><th>Received</th></tr>
      </thead>
      <tbody>${body}</tbody>
    </table>`;
}

async function showDashboard() {
  loginScreen.hidden = true;
  appShell.hidden = false;
  submissionsRegion.innerHTML = `<div class="state-message">Loading…</div>`;
  try {
    const [me, submissions] = await Promise.all([
      api("/agent-portal/me"),
      api("/agent-portal/submissions"),
    ]);
    agentNameEl.textContent = me.display_name || `Agent ${me.telegram_user_id}`;
    renderSubmissions(submissions);
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      returnToLogin("Your session expired -- send /portal to the bot again.");
      return;
    }
    submissionsRegion.innerHTML = `<div class="state-message error">Could not load submissions: ${err.message}</div>`;
  }
}

function returnToLogin(message) {
  appShell.hidden = true;
  loginScreen.hidden = false;
  loginStatus.textContent = message;
  loginStatus.classList.add("error");
}

async function boot() {
  const url = new URL(window.location.href);
  const loginToken = url.searchParams.get("token");

  if (loginToken) {
    loginStatus.textContent = "Signing you in…";
    try {
      const { session_token: sessionToken } = await api("/agent-portal/login", {
        method: "POST",
        body: JSON.stringify({ token: loginToken }),
      });
      setSession(sessionToken);
      url.searchParams.delete("token");
      window.history.replaceState({}, "", url.pathname);
      await showDashboard();
    } catch (err) {
      returnToLogin(
        err instanceof ApiError
          ? "This login link is invalid, expired, or already used -- send /portal to the bot again."
          : `Sign-in failed: ${err.message}`
      );
    }
    return;
  }

  if (getSession()) {
    await showDashboard();
    return;
  }
}

document.getElementById("logout-btn").addEventListener("click", async () => {
  try {
    await api("/agent-portal/logout", { method: "POST" });
  } catch {
    // Best-effort server-side revoke -- clearing the local session below
    // is what actually matters for this device either way.
  }
  clearSession();
  returnToLogin("Logged out. Send /portal to the bot to sign in again.");
  showToast("Logged out");
});

boot();
