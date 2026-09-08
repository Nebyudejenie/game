import { api, escapeHtml } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Rooms";

const WIN_PATTERNS = ["row", "col", "diag"];
const PATTERN_LABELS = { row: "row", col: "column", diag: "diagonal" };
const MIN_WINNING_LINES_MIN = 1;
const MIN_WINNING_LINES_MAX = 4; // matches the DB CHECK constraint exactly

// Shared by the create form and every room's edit form, so an admin sees
// exactly the same plain-language statement of the rule in both places,
// worded the same way this exact combination would be described to a
// player -- never left to infer from raw checkbox/number state alone.
function describeWinningCondition(minLines, patterns) {
  const lineWord = minLines === 1 ? "line" : "lines";
  if (patterns.length === 0) {
    return `${minLines} completed ${lineWord} required -- but no line types are enabled below, so this room can never actually be won. Enable at least one.`;
  }
  const shapes = patterns.map((p) => PATTERN_LABELS[p] || p).join(", ");
  return `${minLines} completed ${lineWord}, in any combination of: ${shapes}.`;
}

function winningConditionPanelHtml(minLines, checkedPatterns) {
  return `
    <label>Required winning lines
      <input
        type="number" name="min_winning_lines" value="${minLines}"
        min="${MIN_WINNING_LINES_MIN}" max="${MIN_WINNING_LINES_MAX}" required
      />
    </label>
    <div class="action-row">
      ${WIN_PATTERNS.map((p) => `
        <label style="flex-direction:row; align-items:center; gap:0.35rem;">
          <input type="checkbox" name="win_patterns" value="${p}" ${checkedPatterns.includes(p) ? "checked" : ""} /> ${PATTERN_LABELS[p]}
        </label>
      `).join("")}
    </div>
    <p class="winning-condition-preview">
      <strong>Winning condition:</strong> <span class="winning-condition-text"></span>
    </p>
  `;
}

// Wires a form's own min_winning_lines input + win_patterns checkboxes to
// keep their shared preview text live -- called once per form right
// after its HTML is inserted, for both create and edit.
function wireWinningConditionPreview(form) {
  const preview = form.querySelector(".winning-condition-text");
  function update() {
    const minLines = Number(form.querySelector('[name="min_winning_lines"]').value) || 1;
    const patterns = Array.from(form.querySelectorAll('[name="win_patterns"]:checked')).map(
      (el) => el.value
    );
    preview.textContent = describeWinningCondition(minLines, patterns);
  }
  form.querySelector('[name="min_winning_lines"]').addEventListener("input", update);
  for (const box of form.querySelectorAll('[name="win_patterns"]')) {
    box.addEventListener("change", update);
  }
  update();
}

export async function render(container) {
  container.innerHTML = `
    <h1>Rooms</h1>
    <div id="rooms-list"><p class="loading">Loading…</p></div>

    <h2>Create room</h2>
    <form id="create-room-form" class="detail-panel">
      <div class="detail-grid">
        <label>Code <input type="text" name="code" required /></label>
        <label>Stake (ETB) <input type="text" name="stake" required value="20.00" /></label>
        <label>House cut (bps) <input type="number" name="house_cut_bps" value="2000" /></label>
        <label>Min players <input type="number" name="min_players" value="2" /></label>
        <label>Max players <input type="number" name="max_players" value="100" /></label>
        <label>Max cards/player <input type="number" name="max_cards_per_player" value="1" min="1" max="20" /></label>
        <label>Lobby seconds <input type="number" name="lobby_seconds" value="30" /></label>
        <label>Call interval (ms) <input type="number" name="call_interval_ms" value="4000" /></label>
        <label>Result seconds <input type="number" name="result_seconds" value="10" /></label>
      </div>
      ${winningConditionPanelHtml(2, WIN_PATTERNS)}
      <div class="action-row">
        <button type="submit" class="btn">Create room</button>
      </div>
    </form>
  `;

  const listEl = container.querySelector("#rooms-list");
  const createForm = container.querySelector("#create-room-form");
  wireWinningConditionPreview(createForm);

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
            <th>ID</th><th>Code</th><th>Stake</th><th>House cut</th><th>Players</th><th>Cards/player</th>
            <th>Lobby (s)</th><th>Call (ms)</th><th>Winning condition</th><th>Active</th><th></th>
          </tr>
        </thead>
        <tbody>
          ${rooms.map((r) => `
            <tr data-room-id="${r.id}">
              <td>${r.id}</td><td>${escapeHtml(r.code)}</td><td>${r.stake} ETB</td>
              <td>${r.house_cut_bps / 100}%</td><td>${r.min_players}–${r.max_players}</td>
              <td>${r.max_cards_per_player}</td>
              <td>${r.lobby_seconds}</td><td>${r.call_interval_ms}</td>
              <td title="${escapeHtml(describeWinningCondition(r.min_winning_lines, r.win_patterns))}">
                ${r.min_winning_lines} completed ${r.min_winning_lines === 1 ? "line" : "lines"}
              </td>
              <td>${r.is_active ? "yes" : "no"}</td>
              <td>
                <button class="btn btn-secondary btn-sm edit-room-btn">Edit</button>
                <button class="btn btn-secondary btn-sm toggle-active-btn">${r.is_active ? "Deactivate" : "Activate"}</button>
                <button class="btn btn-danger btn-sm stop-room-btn">Stop room</button>
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
      row.querySelector(".stop-room-btn").addEventListener("click", () => openStopRoomPanel(roomId));
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
          <label>Max cards/player <input type="number" name="max_cards_per_player" value="${room.max_cards_per_player}" min="1" max="20" /></label>
          <label>Lobby seconds <input type="number" name="lobby_seconds" value="${room.lobby_seconds}" /></label>
          <label>Call interval (ms) <input type="number" name="call_interval_ms" value="${room.call_interval_ms}" /></label>
          <label>Result seconds <input type="number" name="result_seconds" value="${room.result_seconds}" /></label>
        </div>
        ${winningConditionPanelHtml(room.min_winning_lines, room.win_patterns)}
        <div class="action-row">
          <button type="submit" class="btn">Save changes</button>
          <button type="button" class="btn btn-secondary" id="cancel-edit-btn">Cancel</button>
        </div>
      </form>
    `;
    const editForm = panel.querySelector("#edit-room-form");
    wireWinningConditionPreview(editForm);
    panel.querySelector("#cancel-edit-btn").addEventListener("click", () => {
      panel.innerHTML = "";
    });
    editForm.addEventListener("submit", (event) => {
      event.preventDefault();
      saveRoomEdit(roomId, room, event.target);
    });
  }

  async function openStopRoomPanel(roomId) {
    const room = roomsById.get(roomId);
    const panel = listEl.querySelector("#room-edit-panel");
    panel.innerHTML = `<p class="loading">Loading current room state…</p>`;
    let preview;
    try {
      preview = await api(`/rooms/${roomId}/stop-preview`);
    } catch (err) {
      renderError(panel, err);
      return;
    }

    // Real financial consequence, not a guess -- shown before the admin
    // can commit to anything. An idle room with nothing staked gets a
    // plainly different message than an active, money-bearing round.
    const consequence = preview.has_stoppable_round
      ? `This will immediately end round #${preview.current_round_id} and refund ` +
        `${preview.staked_amount} ETB in staked funds to ${preview.players} player(s). ` +
        `The room will also be deactivated so it cannot restart automatically.`
      : `This room has no active round right now -- nothing to refund. ` +
        `The room will be deactivated so it cannot start a new round.`;

    panel.innerHTML = `
      <form id="stop-room-form" class="detail-panel">
        <h2>Stop room #${roomId} (${escapeHtml(preview.room_code)})</h2>
        <div class="detail-grid">
          <div>Current round: <strong>${preview.current_round_id ?? "none"}</strong></div>
          <div>Current state: <strong>${escapeHtml(preview.current_round_status ?? "idle")}</strong></div>
          <div>Players: <strong>${preview.players}</strong></div>
          <div>Staked: <strong>${preview.staked_amount} ETB</strong></div>
        </div>
        <p class="warning-text">${escapeHtml(consequence)}</p>
        <label>Reason (required)
          <input type="text" name="reason" required placeholder="e.g. suspected exploit, safety incident" />
        </label>
        <label>Type STOP to confirm
          <input type="text" name="confirmation" required placeholder="STOP" autocomplete="off" />
        </label>
        <div class="action-row">
          <button type="button" class="btn btn-secondary" id="cancel-stop-btn">Cancel</button>
          <button type="submit" class="btn btn-danger">STOP ROOM</button>
        </div>
      </form>
    `;
    panel.querySelector("#cancel-stop-btn").addEventListener("click", () => {
      panel.innerHTML = "";
    });
    panel.querySelector("#stop-room-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(event.target);
      const reason = String(data.get("reason") || "").trim();
      const confirmation = String(data.get("confirmation") || "").trim();
      if (!reason) {
        toast("A reason is required.", true);
        return;
      }
      if (confirmation.toUpperCase() !== "STOP") {
        toast('Type "STOP" exactly to confirm.', true);
        return;
      }
      try {
        const result = await api(`/rooms/${roomId}/stop`, {
          method: "POST",
          body: { reason, confirmation },
        });
        toast(
          result.stopped_round_id
            ? `Room stopped. Round #${result.stopped_round_id} refunded (${result.refunded_entrants} player(s)).`
            : "Room stopped. No active round to refund."
        );
        panel.innerHTML = "";
        reload();
      } catch (err) {
        toast(err.detail || err.message, true);
      }
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
      max_cards_per_player: Number(data.get("max_cards_per_player")),
      lobby_seconds: Number(data.get("lobby_seconds")),
      call_interval_ms: Number(data.get("call_interval_ms")),
      result_seconds: Number(data.get("result_seconds")),
      win_patterns: winPatterns,
      min_winning_lines: Number(data.get("min_winning_lines")),
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
          max_cards_per_player: Number(data.get("max_cards_per_player")),
          lobby_seconds: Number(data.get("lobby_seconds")),
          call_interval_ms: Number(data.get("call_interval_ms")),
          result_seconds: Number(data.get("result_seconds")),
          win_patterns: winPatterns,
          min_winning_lines: Number(data.get("min_winning_lines")),
        },
      });
      toast("Room created.");
      createForm.reset();
      // form.reset() doesn't reliably re-fire input/change on every
      // browser, which would otherwise leave the winning-condition
      // preview showing the just-submitted values instead of the
      // restored defaults.
      createForm.querySelector('[name="min_winning_lines"]').dispatchEvent(new Event("input"));
      reload();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  });

  await reload();
}
