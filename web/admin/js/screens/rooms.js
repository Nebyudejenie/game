import { api, escapeHtml } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Rooms";

const WIN_PATTERNS = ["row", "col", "diag", "corners"];

export async function render(container) {
  container.innerHTML = `
    <h1>Rooms</h1>
    <div id="rooms-list"><p class="loading">Loading…</p></div>

    <h2>Create room</h2>
    <form id="create-room-form" class="detail-panel">
      <div class="detail-grid">
        <label>Code <input type="text" name="code" required /></label>
        <label>Stake (ETB) <input type="text" name="stake" required placeholder="20.00" /></label>
        <label>House cut (bps) <input type="number" name="house_cut_bps" value="2000" /></label>
        <label>Min players <input type="number" name="min_players" value="2" /></label>
        <label>Max players <input type="number" name="max_players" value="100" /></label>
        <label>Lobby seconds <input type="number" name="lobby_seconds" value="30" /></label>
        <label>Call interval (ms) <input type="number" name="call_interval_ms" value="4000" /></label>
        <label>Result seconds <input type="number" name="result_seconds" value="10" /></label>
      </div>
      <div class="action-row">
        ${WIN_PATTERNS.map((p) => `
          <label style="flex-direction:row; align-items:center; gap:0.35rem;">
            <input type="checkbox" name="win_patterns" value="${p}" checked /> ${p}
          </label>
        `).join("")}
      </div>
      <div class="action-row">
        <button type="submit" class="btn">Create room</button>
      </div>
    </form>
  `;

  const listEl = container.querySelector("#rooms-list");
  const createForm = container.querySelector("#create-room-form");

  async function reload() {
    listEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const rooms = await api("/rooms");
      renderList(rooms);
    } catch (err) {
      renderError(listEl, err);
    }
  }

  // Keyed by room id so an edit form re-render (e.g. after a failed save)
  // can restore exactly the room data it was opened against, not whatever
  // the list happens to hold by the time the user submits.
  let roomsById = new Map();

  function renderList(rooms) {
    roomsById = new Map(rooms.map((r) => [r.id, r]));
    if (rooms.length === 0) {
      listEl.innerHTML = `<p class="empty">No rooms configured.</p>`;
      return;
    }
    listEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>ID</th><th>Code</th><th>Stake</th><th>House cut</th><th>Players</th>
            <th>Lobby (s)</th><th>Call (ms)</th><th>Active</th><th></th>
          </tr>
        </thead>
        <tbody>
          ${rooms.map((r) => `
            <tr data-room-id="${r.id}">
              <td>${r.id}</td><td>${escapeHtml(r.code)}</td><td>${r.stake} ETB</td>
              <td>${r.house_cut_bps / 100}%</td><td>${r.min_players}–${r.max_players}</td>
              <td>${r.lobby_seconds}</td><td>${r.call_interval_ms}</td>
              <td>${r.is_active ? "yes" : "no"}</td>
              <td>
                <button class="btn btn-secondary btn-sm edit-room-btn">Edit</button>
                <button class="btn btn-secondary btn-sm toggle-active-btn">${r.is_active ? "Deactivate" : "Activate"}</button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>
      <div id="room-edit-panel"></div>
    `;
    for (const row of listEl.querySelectorAll("tr[data-room-id]")) {
      const roomId = Number(row.dataset.roomId);
      const isActive = row.querySelector(".toggle-active-btn").textContent.trim() === "Deactivate";
      row.querySelector(".toggle-active-btn").addEventListener("click", () => toggleActive(roomId, isActive));
      row.querySelector(".edit-room-btn").addEventListener("click", () => openEditForm(roomId));
    }
  }

  function openEditForm(roomId) {
    const room = roomsById.get(roomId);
    const panel = listEl.querySelector("#room-edit-panel");
    panel.innerHTML = `
      <form id="edit-room-form" class="detail-panel">
        <h2>Edit room #${roomId} (${escapeHtml(room.code)})</h2>
        <div class="detail-grid">
          <label>Stake (ETB) <input type="text" name="stake" value="${room.stake}" required /></label>
          <label>House cut (bps) <input type="number" name="house_cut_bps" value="${room.house_cut_bps}" /></label>
          <label>Min players <input type="number" name="min_players" value="${room.min_players}" /></label>
          <label>Max players <input type="number" name="max_players" value="${room.max_players}" /></label>
          <label>Lobby seconds <input type="number" name="lobby_seconds" value="${room.lobby_seconds}" /></label>
          <label>Call interval (ms) <input type="number" name="call_interval_ms" value="${room.call_interval_ms}" /></label>
          <label>Result seconds <input type="number" name="result_seconds" value="${room.result_seconds}" /></label>
        </div>
        <div class="action-row">
          ${WIN_PATTERNS.map((p) => `
            <label style="flex-direction:row; align-items:center; gap:0.35rem;">
              <input type="checkbox" name="win_patterns" value="${p}" ${room.win_patterns.includes(p) ? "checked" : ""} /> ${p}
            </label>
          `).join("")}
        </div>
        <div class="action-row">
          <button type="submit" class="btn">Save changes</button>
          <button type="button" class="btn btn-secondary" id="cancel-edit-btn">Cancel</button>
        </div>
      </form>
    `;
    panel.querySelector("#cancel-edit-btn").addEventListener("click", () => {
      panel.innerHTML = "";
    });
    panel.querySelector("#edit-room-form").addEventListener("submit", (event) => {
      event.preventDefault();
      saveRoomEdit(roomId, room, event.target);
    });
  }

  async function saveRoomEdit(roomId, room, form) {
    const data = new FormData(form);
    const winPatterns = data.getAll("win_patterns");
    if (winPatterns.length === 0) {
      toast("Select at least one win pattern.", true);
      return;
    }
    const candidate = {
      stake: data.get("stake"),
      house_cut_bps: Number(data.get("house_cut_bps")),
      min_players: Number(data.get("min_players")),
      max_players: Number(data.get("max_players")),
      lobby_seconds: Number(data.get("lobby_seconds")),
      call_interval_ms: Number(data.get("call_interval_ms")),
      result_seconds: Number(data.get("result_seconds")),
      win_patterns: winPatterns,
    };
    // Only the fields that actually changed -- an admin who just wants to
    // bump one number shouldn't generate an audit-log entry claiming
    // every other field was also "changed" to the value it already had.
    const changes = {};
    for (const [field, value] of Object.entries(candidate)) {
      const before = field === "win_patterns" ? JSON.stringify(room[field]) : String(room[field]);
      const after = field === "win_patterns" ? JSON.stringify(value) : String(value);
      if (before !== after) changes[field] = value;
    }
    if (Object.keys(changes).length === 0) {
      toast("Nothing changed.");
      return;
    }
    const reason = window.prompt(`Reason for editing room #${roomId}:`);
    if (reason === null) return;
    try {
      await api(`/rooms/${roomId}`, { method: "PATCH", body: { changes, reason: reason || null } });
      toast("Room updated.");
      reload();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  async function toggleActive(roomId, currentlyActive) {
    const reason = window.prompt(`Reason to ${currentlyActive ? "deactivate" : "activate"} room #${roomId}:`);
    if (reason === null) return;
    try {
      await api(`/rooms/${roomId}`, {
        method: "PATCH",
        body: { changes: { is_active: !currentlyActive }, reason: reason || null },
      });
      toast("Room updated.");
      reload();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  createForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(createForm);
    const winPatterns = data.getAll("win_patterns");
    if (winPatterns.length === 0) {
      toast("Select at least one win pattern.", true);
      return;
    }
    try {
      await api("/rooms", {
        method: "POST",
        body: {
          code: data.get("code"),
          stake: data.get("stake"),
          house_cut_bps: Number(data.get("house_cut_bps")),
          min_players: Number(data.get("min_players")),
          max_players: Number(data.get("max_players")),
          lobby_seconds: Number(data.get("lobby_seconds")),
          call_interval_ms: Number(data.get("call_interval_ms")),
          result_seconds: Number(data.get("result_seconds")),
          win_patterns: winPatterns,
        },
      });
      toast("Room created.");
      createForm.reset();
      reload();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  });

  await reload();
}
