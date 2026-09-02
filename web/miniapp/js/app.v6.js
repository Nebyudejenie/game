import { initI18n, t, setLanguage, applyServerLanguage } from "./i18n.js";
import { getState, setState, subscribe, serverNow } from "./state.js";
import * as ws from "./ws.js";
import * as haptics from "./haptics.js";
import * as board from "./render/board.js";
import * as card from "./render/card.js";
import { voiceCaller } from "./voice.js";

const tg = window.Telegram && window.Telegram.WebApp;

let winPatterns = ["row", "col", "diag", "corners"];
let countdownTimer = null;
let hasClaimedThisRound = false;

// --- screen management --------------------------------------------------

function showScreen(name) {
  for (const el of document.querySelectorAll(".screen")) {
    el.classList.toggle("active", el.dataset.screen === name);
  }
  setState({ screen: name });
  if (tg) {
    if (name === "rooms") tg.BackButton.hide();
    else tg.BackButton.show();
  }
}

function el(id) {
  return document.getElementById(id);
}

// For the handful of places server-sourced text (an admin-entered
// manual payment destination's name/reference, notably) gets built into
// innerHTML via a template literal rather than set as textContent --
// this closes what would otherwise be a stored-XSS gap if a value ever
// contained "<" (admin-only input, so a low-likelihood path, but a real
// one worth not leaving open just because the blast radius is smaller).
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// Room cards, the card-selection grid, and the AUTO toggle are plain
// <div>s (not <button>s -- they need custom layout/styling <button>'s
// own UA defaults would fight) -- an architecture audit caught that
// left them unreachable without a pointer. tabindex="0" + role="button"
// makes an element real Tab-stop; the keydown handler is what actually
// makes Enter/Space activate it the way a native button already would
// (preventDefault on Space specifically, so it activates the control
// instead of scrolling the page, the one thing a real <button> handles
// for free that this doesn't get automatically).
function makeKeyboardActivatable(element, handler) {
  element.tabIndex = 0;
  if (!element.hasAttribute("role")) element.setAttribute("role", "button");
  element.addEventListener("click", handler);
  element.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      handler();
    }
  });
}

function applyStaticTranslations() {
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n);
  }
  for (const node of document.querySelectorAll("[data-i18n-placeholder]")) {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  }
}

// --- connection banner ----------------------------------------------------

subscribe((state) => {
  const banner = el("connection-banner");
  if (state.connection === "connected") {
    banner.classList.remove("visible");
  } else if (state.connection === "auth_failed") {
    // A code review pass caught that a terminal auth failure (stale
    // initData past Telegram's own validity window, most commonly) used
    // to look identical to an ordinary transient drop -- ws.js retried
    // forever against a handshake that could only ever fail again, and
    // the player just saw an endless "Reconnecting..." with no way to
    // know reloading the app would actually fix it.
    banner.textContent = t("connection.expired");
    banner.classList.add("visible");
  } else if (state.connection === "reconnecting" || state.connection === "offline") {
    banner.textContent = t("connection.reconnecting");
    banner.classList.add("visible");
  } else if (state.connection === "connect_failed") {
    // ws.js gave up after the very first connection attempt could never
    // complete within INITIAL_AUTH_TIMEOUT_MS -- unlike "reconnecting"
    // (which resolves itself once the network recovers), this needs an
    // explicit action: a full page reload re-runs the whole boot()
    // sequence fresh, the same recovery this banner already tells the
    // player to do for an expired session.
    banner.textContent = t("connection.connect_failed");
    banner.classList.add("visible");
  }
  banner.classList.toggle(
    "actionable", state.connection === "auth_failed" || state.connection === "connect_failed"
  );
});

// Both terminal states' own banner text tells the player to reload --
// this is what actually does it. A full reload re-runs boot() fresh,
// which is simpler and more robust than trying to resume a half
// -completed startup sequence in place. No-ops for every other
// connection state (reconnecting/offline resolve themselves).
makeKeyboardActivatable(el("connection-banner"), () => {
  if (["auth_failed", "connect_failed"].includes(getState().connection)) {
    window.location.reload();
  }
});

// --- room list --------------------------------------------------------

function renderRoomList() {
  const state = getState();
  const list = el("room-list");
  list.innerHTML = "";
  if (state.rooms.length === 0) {
    const empty = document.createElement("div");
    empty.className = "wallet-note";
    empty.textContent = t("rooms.empty");
    list.appendChild(empty);
    return;
  }
  for (const room of state.rooms) {
    const card = document.createElement("div");
    card.className = "room-card" + (room.status === "running" ? " playing" : "");
    card.dataset.roomId = String(room.room_id);
    card.innerHTML = `
      <div>
        <div class="stake">${room.stake} ETB</div>
        <div class="players">🟢 ${room.players}</div>
      </div>
      <div style="text-align:right">
        <div class="derash-line">${t("rooms.derash_up_to", { amount: room.pot })}</div>
        <div class="countdown">${roomCountdownText(room)}</div>
      </div>
    `;
    makeKeyboardActivatable(card, () => enterRoom(room.room_id));
    list.appendChild(card);
  }
}

// Mini App spec 2.1's own mockup: "0:18" (a bare countdown) while a room
// is still filling its lobby, the bare word "Playing" once the round's
// started -- "← countdown or 'Playing'" is the diagram's own inline
// label for this exact column. There's no honest way to predict when a
// running round will actually end (that depends entirely on when a
// player completes a pattern, purely random), so this never fabricates
// a number for one -- the spec's separate prose line ("next in ~40s")
// reads as a looser paraphrase of the same row, not a literal formula
// this could compute without guessing.
function roomCountdownText(room) {
  if (room.status === "lobby" && room.lobby_deadline_ms != null) {
    const secondsLeft = Math.max(0, Math.round((room.lobby_deadline_ms - serverNow()) / 1000));
    const minutes = Math.floor(secondsLeft / 60);
    const seconds = String(secondsLeft % 60).padStart(2, "0");
    return `${minutes}:${seconds}`;
  }
  if (room.status === "running") return t("rooms.playing_now");
  return "";
}

ws.on("rooms", (msg) => {
  setState({ rooms: msg.rooms });
  if (getState().screen === "rooms") renderRoomList();
});

function refreshRoomList() {
  ws.requestRooms();
}

// The room list only gets a fresh "rooms" push on request (boot, or
// returning here after a round ends) -- without this, a lobby room's
// countdown would sit frozen at whatever number happened to be true the
// last time the list was fetched, indistinguishable from a bug to a
// player watching it. Same guarded-permanent-interval shape as the
// session-time reminder below; updates the existing room data in-memory
// (no new request) so lobby countdowns actually tick.
//
// This used to call renderRoomList() itself, which wipes and rebuilds
// every card via innerHTML = "" every single second -- destroying and
// replacing the exact DOM node a keyboard user just tabbed to, silently
// kicking focus back to <body> once a second. Ticking only the countdown
// text in place keeps the existing nodes (and their focus/listeners)
// alive; a full rebuild is still what happens whenever genuinely new
// room data arrives above.
setInterval(() => {
  if (getState().screen === "rooms") tickRoomCountdowns();
}, 1000);

function tickRoomCountdowns() {
  const rooms = getState().rooms;
  for (const roomEl of el("room-list").children) {
    const room = rooms.find((r) => String(r.room_id) === roomEl.dataset.roomId);
    if (!room) continue;
    const countdownEl = roomEl.querySelector(".countdown");
    if (countdownEl) countdownEl.textContent = roomCountdownText(room);
  }
}

// --- entering a room: state_sync decides which screen to show -----------

function enterRoom(roomId) {
  hasClaimedThisRound = false;
  // Earliest reliable user gesture in the join flow -- satisfies mobile
  // Safari / Telegram WebView's autoplay policy for every announce()
  // later in this session, without needing a gesture on every call.
  voiceCaller.unlock();
  ws.joinRoom(roomId);
}

ws.on("state_sync", (msg) => {
  setState({ round: msg });
  winPatterns = msg.win_patterns || winPatterns;

  if (msg.status === "idle" || msg.status === "voided" || msg.status === "done") {
    showScreen("rooms");
    refreshRoomList();
  } else if (msg.status === "lobby") {
    enterLobby(msg);
  } else if (msg.status === "running" || msg.status === "settling") {
    if (msg.your_card) {
      enterGame(msg);
    } else {
      enterSpectate(msg);
    }
  }
});

// --- lobby (card selection) --------------------------------------------

let selectedCard = null;
const takenCards = new Set();

function enterLobby(sync) {
  showScreen("lobby");
  selectedCard = sync.your_card || null;
  takenCards.clear();
  buildCardGrid();
  updateLobbyCta();
  startLobbyCountdown(sync);
}

function buildCardGrid() {
  const grid = el("card-grid");
  grid.innerHTML = "";
  for (let n = 1; n <= 100; n++) {
    const cell = document.createElement("div");
    cell.className = "card-grid-cell";
    cell.textContent = String(n);
    makeKeyboardActivatable(cell, () => {
      if (takenCards.has(n) && n !== selectedCard) return;
      haptics.lightTap();
      selectedCard = n;
      renderCardGridState();
      updateLobbyCta();
    });
    grid.appendChild(cell);
  }
  renderCardGridState();
}

function renderCardGridState() {
  for (const cellEl of el("card-grid").children) {
    const n = Number(cellEl.textContent);
    cellEl.classList.toggle("taken", takenCards.has(n) && n !== selectedCard);
    cellEl.classList.toggle("selected", n === selectedCard);
  }
}

function updateLobbyCta() {
  const cta = el("lobby-cta");
  const state = getState();
  const stake = state.round ? state.round.stake : "";
  const alreadyMine = state.round && state.round.your_card === selectedCard;

  if (selectedCard === null) {
    cta.textContent = t("lobby.pick_card");
    cta.disabled = true;
    return;
  }
  cta.disabled = false;
  cta.textContent = alreadyMine
    ? t("lobby.card_taken_change", { card: selectedCard })
    : t("lobby.take_card", { card: selectedCard, stake });
}

el("lobby-cta").addEventListener("click", () => {
  const state = getState();
  if (selectedCard === null || !state.currentRoomId) return;
  if (state.round && state.round.your_card === selectedCard) return; // already committed
  haptics.mediumTap();
  ws.takeCard(state.currentRoomId, selectedCard);
});

ws.on("card_taken", (msg) => {
  if (msg.taken) takenCards.add(msg.card_no);
  else takenCards.delete(msg.card_no);
  if (getState().screen === "lobby") renderCardGridState();
});

ws.on("ack", (msg) => {
  if (!msg.ok) {
    showToast(msg.reason || "error.generic");
    return;
  }
  if (msg.for === "take_card") {
    haptics.success();
    // The ack only confirms success -- it doesn't carry the card's actual
    // grid (the command channel's replies are deliberately {ok, reason}
    // only). Re-requesting state_sync is what actually populates
    // your_card_grid, which enterGame() needs once the round starts;
    // patching only `your_card` locally left it null and crashed
    // setCardGrid() the moment round_start fired (a real bug an E2E test
    // caught -- see DECISIONS.md).
    const state = getState();
    if (state.currentRoomId !== null) ws.joinRoom(state.currentRoomId);
    // enterLobby(), triggered by the state_sync reply just requested
    // above, is what refreshes the CTA text correctly (it has the fresh
    // your_card by then); no need to also do it here against stale state.
  }
});

function startLobbyCountdown(sync) {
  clearInterval(countdownTimer);
  const label = el("lobby-countdown");
  countdownTimer = setInterval(() => {
    const secondsLeft = Math.max(0, Math.round((sync.lobby_deadline_ms - serverNow()) / 1000));
    label.textContent = t("lobby.starts_in", { seconds: secondsLeft });
  }, 250);
}

ws.on("lobby_tick", (msg) => {
  if (getState().screen !== "lobby") return;
  el("lobby-countdown").textContent = t("lobby.starts_in", { seconds: msg.seconds_left });
});

// --- game screen (RUNNING) ---------------------------------------------

function enterGame(sync) {
  showScreen("game");
  hasClaimedThisRound = false;
  board.buildBoard(el("board"));
  card.buildCard(el("your-card"));
  card.setCardGrid(sync.your_card_grid);
  board.setYourCardNumbers(sync.your_card_grid ? gridNumbers(sync.your_card_grid) : []);
  board.markAllCalled(sync.called || []);
  card.markCalledOnCard(new Set(sync.called || []));
  updateStatStrip(sync);
  updateBingoButton(new Set(sync.called || []));

  const autoOn = sync.auto_mark !== false;
  setState({ autoMark: autoOn });
  el("auto-switch").classList.toggle("on", autoOn);
  el("auto-switch").setAttribute("aria-checked", String(autoOn));

  card.onCellClick((r, c) => {
    if (getState().autoMark) return;
    // Optimistic local mark only -- the server never trusts this; it
    // always recomputes from its own called-numbers set (spec principle 1).
    haptics.lightTap();
  });
}

function gridNumbers(grid) {
  const numbers = [];
  for (const row of grid) for (const v of row) if (v !== 0) numbers.push(v);
  return numbers;
}

function updateStatStrip(sync) {
  el("stat-derash").textContent = `${sync.derash || sync.pot || "0.00"} ETB`;
  el("stat-players").textContent = String(sync.players || "");
  el("stat-stake").textContent = `${sync.stake || ""} ETB`;
  el("stat-call").textContent = `${sync.call_index || 0}/75`;
}

ws.on("round_start", (sync) => {
  // One merge, reused everywhere below -- round_start's own payload
  // doesn't carry `stake` (only round.js/state_sync do), and `called`
  // must actually reset to [] for the new round, not just look reset to
  // whatever enterGame() was locally handed. Splitting these into
  // separate ad-hoc merges previously left the stat strip's stake blank
  // after the first round_start of a session, and left state.round.called
  // stale across rounds (caught by review of a real E2E screenshot, not
  // by a unit test -- see DECISIONS.md).
  const merged = { ...getState().round, ...sync, called: [] };
  setState({ round: merged });
  voiceCaller.resetRound();
  if (getState().screen === "lobby" || getState().screen === "game") {
    if (merged.your_card) enterGame(merged);
    else enterSpectate(merged);
  }
  updateStatStrip(merged);
});

ws.on("call", (msg) => {
  const state = getState();
  if (state.screen !== "game" && state.screen !== "lobby") return;
  board.markCalled(msg.number);
  const calledSoFar = new Set([...(state.round ? state.round.called || [] : []), msg.number]);
  setState({ round: { ...state.round, called: [...calledSoFar], call_index: msg.index } });
  card.markCalledOnCard(calledSoFar);

  const badge = el("call-badge");
  badge.textContent = `${msg.letter}${msg.number}`;
  badge.dataset.letter = msg.letter;
  badge.classList.remove("show");
  void badge.offsetWidth; // restart the CSS animation
  badge.classList.add("show");
  haptics.mediumTap();
  voiceCaller.announce(msg.letter, msg.number, msg.index);

  pushRecentCall(msg);
  el("stat-call").textContent = `${msg.index}/75`;
  updateBingoButton(calledSoFar);
});

function pushRecentCall(msg) {
  const recent = el("recent-calls");
  const chip = document.createElement("span");
  chip.textContent = `${msg.letter}${msg.number}`;
  chip.dataset.letter = msg.letter;
  recent.insertBefore(chip, recent.firstChild);
  while (recent.children.length > 3) recent.removeChild(recent.lastChild);
}

function updateBingoButton(calledSet) {
  const btn = el("bingo-btn");
  const complete = card.hasCompletePattern(calledSet, winPatterns);
  btn.disabled = !complete || hasClaimedThisRound;

  const state = getState();
  if (state.autoMark && complete && !hasClaimedThisRound && state.round && state.round.round_id) {
    hasClaimedThisRound = true;
    ws.claim(state.round.round_id);
  }
}

el("bingo-btn").addEventListener("click", () => {
  const state = getState();
  if (el("bingo-btn").disabled || !state.round || !state.round.round_id) return;
  hasClaimedThisRound = true;
  ws.claim(state.round.round_id);
});

// role="switch" (not the generic "button" makeKeyboardActivatable
// defaults to), since this is a real on/off toggle -- set before calling
// it, which the helper takes as "already has a role, don't override".
el("auto-switch").setAttribute("role", "switch");
el("auto-switch").setAttribute("aria-checked", "true"); // matches index.html's own static class="switch on" default
makeKeyboardActivatable(el("auto-switch"), () => {
  const state = getState();
  const next = !state.autoMark;
  setState({ autoMark: next });
  el("auto-switch").classList.toggle("on", next);
  el("auto-switch").setAttribute("aria-checked", String(next));
  if (state.currentRoomId !== null) ws.setAuto(state.currentRoomId, next);
});

// --- voice caller settings -------------------------------------------
// localStorage, not the server-synced pattern auto_mark uses above --
// this is a per-device audio-playback preference with zero gameplay
// impact (a player reasonably wants different volume on phone vs.
// desktop), so there's no server-side consumer for it to round-trip
// through. See DECISIONS.md for why this is a deliberate exception.

const VOICE_STORAGE_KEYS = {
  enabled: "jobingo_voice_enabled",
  volume: "jobingo_voice_volume",
  speed: "jobingo_voice_speed",
};

function loadVoiceSettings() {
  try {
    const enabled = localStorage.getItem(VOICE_STORAGE_KEYS.enabled);
    const volume = localStorage.getItem(VOICE_STORAGE_KEYS.volume);
    const speed = localStorage.getItem(VOICE_STORAGE_KEYS.speed);
    if (enabled !== null) voiceCaller.setEnabled(enabled === "true");
    if (volume !== null) voiceCaller.setVolume(parseFloat(volume));
    if (speed !== null) voiceCaller.setSpeed(parseFloat(speed));
  } catch {
    // Private-mode/unavailable localStorage -- voice defaults stand,
    // never blocks boot.
  }
}

function saveVoiceSetting(key, value) {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    // Best-effort; nothing to recover from here.
  }
}

function syncVoiceToggleUI() {
  const on = voiceCaller.isEnabled();
  for (const id of ["voice-switch", "voice-switch-settings"]) {
    const node = el(id);
    node.classList.toggle("on", on);
    node.setAttribute("aria-checked", String(on));
  }
}

function setVoiceEnabled(next) {
  voiceCaller.setEnabled(next);
  saveVoiceSetting(VOICE_STORAGE_KEYS.enabled, next);
  syncVoiceToggleUI();
}

loadVoiceSettings();

for (const id of ["voice-switch", "voice-switch-settings"]) {
  el(id).setAttribute("role", "switch");
  makeKeyboardActivatable(el(id), () => setVoiceEnabled(!voiceCaller.isEnabled()));
}
syncVoiceToggleUI();

el("voice-volume-slider").value = String(Math.round(voiceCaller.getVolume() * 100));
el("voice-volume-slider").addEventListener("input", () => {
  const volume = Number(el("voice-volume-slider").value) / 100;
  voiceCaller.setVolume(volume);
  saveVoiceSetting(VOICE_STORAGE_KEYS.volume, volume);
});

el("voice-speed-slider").value = String(Math.round(voiceCaller.getSpeed() * 100));
el("voice-speed-slider").addEventListener("input", () => {
  const speed = Number(el("voice-speed-slider").value) / 100;
  voiceCaller.setSpeed(speed);
  saveVoiceSetting(VOICE_STORAGE_KEYS.speed, speed);
});

el("voice-replay-btn").addEventListener("click", () => voiceCaller.replayLast());

el("voice-settings-btn").addEventListener("click", () => {
  el("voice-settings-panel").classList.toggle("hidden");
});

ws.on("claim_result", (msg) => {
  if (msg.valid) return; // the eventual round_end message drives the UI
  hasClaimedThisRound = false;
  const btn = el("bingo-btn");
  btn.classList.remove("shake");
  void btn.offsetWidth;
  btn.classList.add("shake");
  haptics.warning();
  showToast("game.no_pattern_yet");
});

// --- spectate ------------------------------------------------------------

function enterSpectate(sync) {
  showScreen("game");
  el("spectate-banner").classList.remove("hidden");
  el("your-card-section").classList.add("hidden");
  board.buildBoard(el("board"));
  board.markAllCalled(sync.called || []);
  updateStatStrip(sync);
}

el("reserve-card-btn").addEventListener("click", () => {
  const state = getState();
  if (state.currentRoomId !== null) enterRoom(state.currentRoomId);
});

// --- result (SETTLING) --------------------------------------------------

ws.on("round_end", (msg) => {
  el("spectate-banner").classList.add("hidden");
  el("your-card-section").classList.remove("hidden");
  el("fairness-panel").classList.add("hidden");
  setState({ lastResult: msg });

  const state = getState();
  const userId = state.user ? state.user.id : null;
  const mine = (msg.winners || []).find((w) => w.user_id === userId);
  const stake = state.round ? state.round.stake : "0";
  // Spectators (joined with no card) never staked this round -- don't
  // touch the reality-check total for them.
  const participated = Boolean(state.round && state.round.your_card);

  if (participated) {
    const delta = mine
      ? parseFloat(mine.amount)
      : (msg.winners || []).length > 0
        ? -parseFloat(stake)
        : 0; // no winner: the stake was refunded, net zero
    setState({ sessionNetPosition: state.sessionNetPosition + delta });
  }

  showScreen("result");
  const shown = mine || (msg.winners || [])[0] || null;
  if (mine) {
    el("result-title").textContent = t("result.win_title");
    el("result-title").classList.add("win");
    el("result-amount").textContent = `+ ${mine.amount} ETB`;
    el("result-meta").textContent = t("result.card_row", { card: mine.card_no, pattern: mine.pattern });
    haptics.success();
  } else if (shown) {
    // A winner identifier, not just a bare amount -- every other player
    // in the room previously only ever saw "someone won this much,"
    // with no sense of who. display_name is the same public identity
    // this codebase already shows a player to everyone else (admin
    // console, bot messages), not new exposure.
    el("result-title").textContent = shown.display_name
      ? t("result.other_winner", { name: shown.display_name })
      : "";
    el("result-title").classList.remove("win");
    el("result-amount").textContent = `${shown.amount} ETB`;
    el("result-meta").textContent = t("result.card_row", { card: shown.card_no, pattern: shown.pattern });
  } else {
    el("result-title").textContent = "";
    el("result-title").classList.remove("win");
    el("result-amount").textContent = t("result.no_winner");
    el("result-meta").textContent = "";
  }

  // The actual winning card, not just a text summary -- spec: "Render a
  // proper Bingo card preview," winning cells strongly highlighted.
  // Every winner in this broadcast carries their own grid (round_engine
  // .py's already-in-memory card_pool), so this works identically for
  // "I won" and "someone else won" -- no separate code path needed.
  if (shown && shown.grid) {
    card.renderStaticCard(el("result-card"), shown.grid, state.round ? state.round.called : [], shown.pattern);
    el("result-card-panel").classList.remove("hidden");
  } else {
    el("result-card-panel").classList.add("hidden");
  }
  renderSessionTotal();
});

// Reality check (spec section 12): "net position this session, shown
// plainly" -- right on the results screen, every time, not buried in the
// wallet where a losing player has no reason to go look for it.
function renderSessionTotal() {
  const total = getState().sessionNetPosition;
  const node = el("result-session");
  const sign = total > 0 ? "+" : total < 0 ? "-" : "";
  node.textContent = t("result.session_total", { sign, amount: Math.abs(total).toFixed(2) });
  node.classList.remove("positive", "negative");
  if (total > 0) node.classList.add("positive");
  else if (total < 0) node.classList.add("negative");
}

el("play-next-btn").addEventListener("click", () => {
  const state = getState();
  if (state.currentRoomId !== null) enterRoom(state.currentRoomId);
  else showScreen("rooms");
});

// --- provably-fair verification ------------------------------------------
// Spec section 14 (definition of done): "a player can independently verify
// any round's draw from the published seed." The server_seed is only ever
// revealed once a round is terminal (round_engine.py persists it at that
// point, not before -- see DECISIONS.md, Phase 7); this panel is that
// reveal, plus the app's own recomputation of it, surfaced to the player.

el("verify-draw-btn").addEventListener("click", async () => {
  const result = getState().lastResult;
  const panel = el("fairness-panel");
  if (!result || result.round_id == null) return;

  el("fairness-verified").textContent = "";
  el("fairness-hash").textContent = "";
  el("fairness-seed").textContent = "";
  panel.classList.remove("hidden");

  try {
    const response = await fetch(`/api/rounds/${result.round_id}/fairness`, { headers: authHeader() });
    if (!response.ok) {
      el("fairness-verified").textContent = t("fairness.error");
      return;
    }
    const data = await response.json();
    if (!data.revealed) {
      el("fairness-verified").textContent = t("fairness.not_yet");
      el("fairness-hash").textContent = data.server_seed_hash || "";
      return;
    }
    const verifiedEl = el("fairness-verified");
    verifiedEl.textContent = data.verified ? t("fairness.yes") : t("fairness.no");
    verifiedEl.className = data.verified ? "verified-yes" : "verified-no";
    el("fairness-hash").textContent = data.server_seed_hash;
    el("fairness-seed").textContent = data.server_seed;
  } catch {
    el("fairness-verified").textContent = t("fairness.error");
  }
});

el("fairness-close-btn").addEventListener("click", () => {
  el("fairness-panel").classList.add("hidden");
});

// --- wallet --------------------------------------------------------------

function applyWalletTheme() {
  const wallet = el("screen-wallet");
  if (tg && tg.themeParams) {
    const p = tg.themeParams;
    if (p.bg_color) wallet.style.setProperty("--bg", p.bg_color);
    if (p.secondary_bg_color) wallet.style.setProperty("--surface", p.secondary_bg_color);
    if (p.text_color) wallet.style.setProperty("--text", p.text_color);
    if (p.hint_color) wallet.style.setProperty("--muted", p.hint_color);
    if (p.button_color) wallet.style.setProperty("--accent", p.button_color);
  }
}

async function openWallet() {
  showScreen("wallet");
  applyWalletTheme();
  try {
    const response = await fetch("/api/me", { headers: authHeader() });
    if (response.ok) {
      const data = await response.json();
      el("wallet-cash").textContent = `${data.cash} ETB`;
      el("wallet-bonus").textContent = `${data.bonus} ETB`;
      el("wallet-locked").textContent = `${data.locked} ETB`;
    }
  } catch {
    /* wallet screen just shows whatever it already had */
  }
  await applyPaymentAvailability();
}

// P1: the backend, not this file, decides which rail is live --
// payment_provider_availability, read fresh every time the wallet
// opens, so an admin flipping a toggle takes effect for the very next
// player who opens their wallet, not just on a future deploy.
async function applyPaymentAvailability() {
  try {
    const response = await fetch("/api/payment-methods", { headers: authHeader() });
    if (!response.ok) return;
    const methods = await response.json();

    const depositHasAutomatic = methods.deposit.includes("chapa");
    const depositHasManual = methods.deposit.includes("manual");
    if (!depositHasAutomatic) {
      // Nothing to toggle *from* -- go straight to the manual panel,
      // permanently, rather than showing a toggle button that would
      // only ever lead to a dead automatic form.
      el("deposit-automatic-section").classList.add("hidden");
      el("deposit-manual-toggle-btn").classList.add("hidden");
      el("deposit-automatic-toggle-btn").classList.add("hidden");
      if (depositHasManual) {
        el("deposit-manual-section").classList.remove("hidden");
        if (!manualDestinationsLoaded) {
          manualDestinationsLoaded = true;
          await loadManualDestinations();
        }
      } else {
        el("deposit-manual-section").classList.add("hidden");
        setWalletStatus("deposit-status", "wallet.not_available", "error");
      }
    } else if (!depositHasManual) {
      // Automatic works but manual doesn't (or isn't configured) --
      // never offer a toggle to a dead end.
      el("deposit-manual-toggle-btn").classList.add("hidden");
    }

    const withdrawHasAutomatic = methods.withdraw.includes("chapa");
    const withdrawHasManual = methods.withdraw.includes("manual");
    const manualToggleRow = el("withdraw-manual-toggle-row");
    if (!withdrawHasAutomatic && withdrawHasManual) {
      // Only manual works -- every withdrawal is manual regardless of
      // the checkbox, so lock it checked and hide the now-meaningless
      // choice rather than leave a togglable control with only one
      // real answer.
      el("withdraw-manual-checkbox").checked = true;
      el("withdraw-manual-checkbox").disabled = true;
      manualToggleRow.classList.add("hidden");
    } else if (!withdrawHasAutomatic && !withdrawHasManual) {
      setWalletStatus("withdraw-status", "wallet.not_available", "error");
      el("withdraw-submit-btn").disabled = true;
    } else if (!withdrawHasManual) {
      manualToggleRow.classList.add("hidden");
    }
  } catch {
    /* wallet screen just shows whatever it already had -- the submit
       handlers' own error paths still catch a genuinely unavailable
       provider server-side either way. */
  }
}

function authHeader() {
  const raw = tg ? tg.initData : "";
  return raw ? { Authorization: `tma ${raw}` } : {};
}

function setWalletStatus(id, key, kind) {
  const node = el(id);
  node.textContent = key ? t(key) : "";
  node.classList.remove("error", "success");
  if (kind) node.classList.add(kind);
}

document.querySelectorAll(".wallet-tab").forEach((tabEl) => {
  tabEl.addEventListener("click", () => {
    document.querySelectorAll(".wallet-tab").forEach((t2) => t2.classList.remove("active"));
    tabEl.classList.add("active");
    document.querySelectorAll(".wallet-pane").forEach((pane) => pane.classList.add("hidden"));
    el(`wallet-pane-${tabEl.dataset.tab}`).classList.remove("hidden");
    if (tabEl.dataset.tab === "history") loadHistory();
  });
});

el("open-wallet-btn").addEventListener("click", openWallet);
el("wallet-back-btn").addEventListener("click", () => showScreen("rooms"));

// --- deposit ---------------------------------------------------------
// Spec 2.6: "On return: 'Confirming your deposit…' with live polling,
// never a premature success." Opening the checkout link is not a
// completion -- the player hasn't paid yet at that point -- so this
// tracks the deposit as pending and only ever calls it done once the
// real ledger credit actually lands. `balance_update` (already pushed
// live over this user's own Redis channel the moment
// services/payments/deposits.py posts the credit -- see the handler
// below) stands in for polling: genuinely live, and no new backend
// endpoint needed. Comparing the new cash figure against what it was
// before this specific deposit, by at least the deposited amount, is
// what keeps this honest against an unrelated balance_update (a
// same-session round settling, an admin adjustment) arriving while a
// deposit happens to be pending and getting mislabeled as this deposit's
// own confirmation.
let pendingDeposit = null;

document.querySelectorAll("#deposit-amount-chips .amount-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll("#deposit-amount-chips .amount-chip").forEach((c) => c.classList.remove("selected"));
    chip.classList.add("selected");
    el("deposit-amount-input").value = chip.dataset.amount;
  });
});

el("deposit-submit-btn").addEventListener("click", async () => {
  const amount = el("deposit-amount-input").value;
  if (!amount || Number(amount) <= 0) {
    setWalletStatus("deposit-status", "wallet.error.invalid_amount", "error");
    return;
  }
  el("deposit-submit-btn").disabled = true;
  setWalletStatus("deposit-status", "wallet.deposit_opening", null);
  try {
    const response = await fetch("/api/deposit", {
      method: "POST",
      headers: { ...authHeader(), "Content-Type": "application/json" },
      body: JSON.stringify({ amount }),
    });
    if (response.status === 503) {
      setWalletStatus("deposit-status", "wallet.not_available", "error");
      return;
    }
    const data = await response.json();
    if (!response.ok) {
      setWalletStatus("deposit-status", `wallet.error.${data.detail || "generic"}`, "error");
      return;
    }
    if (tg) tg.openLink(data.checkout_url);
    else window.open(data.checkout_url, "_blank");
    pendingDeposit = { amount: Number(amount), cashBefore: Number(getState().user?.balance ?? 0) };
    setWalletStatus("deposit-status", "wallet.deposit_confirming", null);
  } catch {
    setWalletStatus("deposit-status", "wallet.error.generic", "error");
  } finally {
    el("deposit-submit-btn").disabled = false;
  }
});

// --- manual deposit (P1: keep taking deposits when the automatic
// provider is unavailable) -- a distinct panel, not another automatic
// -deposit field: the flow is genuinely different (pick a destination,
// pay externally, come back with a reference), not a variant of the
// same form.

let manualDestinationsLoaded = false;

el("deposit-manual-toggle-btn").addEventListener("click", async () => {
  el("deposit-automatic-section").classList.add("hidden");
  el("deposit-manual-toggle-btn").classList.add("hidden");
  el("deposit-manual-section").classList.remove("hidden");
  el("deposit-automatic-toggle-btn").classList.remove("hidden");
  if (!manualDestinationsLoaded) {
    manualDestinationsLoaded = true;
    await loadManualDestinations();
  }
});

el("deposit-automatic-toggle-btn").addEventListener("click", () => {
  el("deposit-manual-section").classList.add("hidden");
  el("deposit-automatic-toggle-btn").classList.add("hidden");
  el("deposit-automatic-section").classList.remove("hidden");
  el("deposit-manual-toggle-btn").classList.remove("hidden");
});

// method_kind values a real admin can enter (services/admin/queries.py's
// create_manual_payment_destination_admin takes a free-ish string, not a
// closed enum) -- a plain bank emoji covers anything not in this small
// "recognizable brand" list rather than leaving an icon slot blank.
const DESTINATION_ICONS = { telebirr: "📱", cbe_birr: "🏦", cbe: "🏦", awash: "🏦", boa: "🏦" };

let selectedManualDestinationId = null;

async function loadManualDestinations() {
  const listEl = el("deposit-manual-destinations");
  try {
    const response = await fetch("/api/manual-payment-destinations", { headers: authHeader() });
    const destinations = await response.json();
    if (!response.ok || destinations.length === 0) {
      listEl.innerHTML = `<p class="wallet-note">${t("wallet.no_manual_destinations")}</p>`;
      return;
    }
    selectedManualDestinationId = destinations[0].id;
    listEl.innerHTML = `
      <div class="destination-list" id="deposit-manual-destination-list">
        ${destinations
          .map(
            (d) => `
          <button type="button" class="destination-card" data-id="${d.id}"
                  aria-pressed="${d.id === selectedManualDestinationId}">
            <span class="destination-icon">${DESTINATION_ICONS[d.method_kind] || "🏦"}</span>
            <span class="destination-info">
              <span class="destination-name">${escapeHtml(d.method_kind.replace(/_/g, " "))}</span>
              <span class="destination-ref">${escapeHtml(d.account_name)} · ${escapeHtml(d.account_ref)}</span>
            </span>
            <span class="destination-check"></span>
          </button>`
          )
          .join("")}
      </div>
      <p id="deposit-manual-instructions" class="wallet-note"></p>
    `;
    const instructionsEl = el("deposit-manual-instructions");
    const byId = new Map(destinations.map((d) => [d.id, d]));
    const updateInstructions = () => {
      const destination = byId.get(selectedManualDestinationId);
      instructionsEl.textContent = (destination && destination.instructions) || "";
    };
    // Real <button>s -- already fully keyboard-accessible (Tab + Enter/
    // Space) with no extra wiring, unlike the plain <div>s
    // makeKeyboardActivatable exists for elsewhere in this file.
    for (const card of listEl.querySelectorAll(".destination-card")) {
      card.addEventListener("click", () => {
        selectedManualDestinationId = Number(card.dataset.id);
        for (const other of listEl.querySelectorAll(".destination-card")) {
          other.setAttribute("aria-pressed", String(other === card));
        }
        updateInstructions();
      });
    }
    updateInstructions();
  } catch {
    listEl.innerHTML = `<p class="wallet-note">${t("wallet.error.generic")}</p>`;
  }
}

el("deposit-manual-submit-btn").addEventListener("click", async () => {
  const amount = el("deposit-manual-amount-input").value;
  const reference = el("deposit-manual-reference-input").value.trim();
  const destinationId = selectedManualDestinationId;
  if (!amount || Number(amount) <= 0 || !destinationId || !reference) {
    setWalletStatus("deposit-manual-status", "wallet.error.invalid_amount", "error");
    return;
  }
  el("deposit-manual-submit-btn").disabled = true;
  setWalletStatus("deposit-manual-status", "wallet.deposit_opening", null);
  try {
    const response = await fetch("/api/deposit/manual", {
      method: "POST",
      headers: { ...authHeader(), "Content-Type": "application/json" },
      body: JSON.stringify({ amount, manual_destination_id: destinationId, external_reference: reference }),
    });
    const data = await response.json();
    if (!response.ok) {
      setWalletStatus("deposit-manual-status", `wallet.error.${data.detail || "generic"}`, "error");
      return;
    }
    setWalletStatus("deposit-manual-status", "wallet.manual_deposit_submitted", "success");
    el("deposit-manual-amount-input").value = "";
    el("deposit-manual-reference-input").value = "";
  } catch {
    setWalletStatus("deposit-manual-status", "wallet.error.generic", "error");
  } finally {
    el("deposit-manual-submit-btn").disabled = false;
  }
});

// --- withdraw ----------------------------------------------------------
// The summary card is a pure, read-only reflection of the three inputs
// below it plus the request's own outcome -- no new data, nothing the
// submit handler doesn't already have. Kept live via "input" listeners
// so it updates as the player types, matching the reference mockups'
// own "review before you submit" pattern.

function setWithdrawSummaryStatus(text, kind) {
  const pill = el("withdraw-summary-status");
  pill.textContent = text;
  pill.classList.remove("pending", "success");
  if (kind) pill.classList.add(kind);
}

function updateWithdrawSummary() {
  const amount = el("withdraw-amount-input").value;
  const accountRef = el("withdraw-account-input").value.trim();
  const holderName = el("withdraw-name-input").value.trim();
  el("withdraw-summary-amount").textContent = `${amount || 0} ETB`;
  el("withdraw-summary-account").textContent = accountRef || t("wallet.summary_not_provided");
  el("withdraw-summary-holder").textContent = holderName || t("wallet.summary_not_provided");
}

for (const id of ["withdraw-amount-input", "withdraw-account-input", "withdraw-name-input"]) {
  el(id).addEventListener("input", updateWithdrawSummary);
}

el("withdraw-submit-btn").addEventListener("click", async () => {
  const amount = el("withdraw-amount-input").value;
  const accountRef = el("withdraw-account-input").value.trim();
  const holderName = el("withdraw-name-input").value.trim();
  const manual = el("withdraw-manual-checkbox").checked;
  if (!amount || Number(amount) <= 0 || !accountRef || !holderName) {
    setWalletStatus("withdraw-status", "wallet.error.invalid_amount", "error");
    return;
  }
  el("withdraw-submit-btn").disabled = true;
  setWalletStatus("withdraw-status", "wallet.withdraw_submitting", null);
  setWithdrawSummaryStatus(t("wallet.withdraw_submitting"), "pending");
  try {
    const response = await fetch("/api/withdraw", {
      method: "POST",
      headers: { ...authHeader(), "Content-Type": "application/json" },
      body: JSON.stringify({
        amount,
        account_ref: accountRef,
        holder_name: holderName,
        provider: manual ? "manual" : "chapa",
      }),
    });
    if (response.status === 503) {
      setWalletStatus("withdraw-status", "wallet.not_available", "error");
      setWithdrawSummaryStatus(t("wallet.summary_not_submitted"), null);
      return;
    }
    const data = await response.json();
    if (!response.ok) {
      setWalletStatus("withdraw-status", `wallet.error.${data.detail || "generic"}`, "error");
      setWithdrawSummaryStatus(t("wallet.summary_not_submitted"), null);
      return;
    }
    const approved = data.status === "approved";
    setWalletStatus("withdraw-status", approved ? "wallet.withdraw_approved" : "wallet.withdraw_review", "success");
    setWithdrawSummaryStatus(
      approved ? t("wallet.summary_approved") : t("wallet.summary_pending_approval"),
      approved ? "success" : "pending"
    );
  } catch {
    setWalletStatus("withdraw-status", "wallet.error.generic", "error");
    setWithdrawSummaryStatus(t("wallet.summary_not_submitted"), null);
  } finally {
    el("withdraw-submit-btn").disabled = false;
  }
});

// --- history -------------------------------------------------------------
// Spec 2.6: "History: rounds and transactions, filterable, each linking
// to its detail." Transactions and per-round detail links aren't built
// yet (no transaction-listing endpoint or round-detail screen exists),
// but the filter itself is a real, self-contained gap: the last 10
// rounds fetched here already carry a won/lost outcome, so filtering by
// it needs no new backend surface -- reusing the deposit screen's own
// .amount-chip look for a native, already-keyboard-accessible <button>
// rather than inventing a new control style.

let historyRows = [];
let historyFilter = "all";

async function loadHistory() {
  try {
    const response = await fetch("/api/history", { headers: authHeader() });
    if (!response.ok) return;
    historyRows = await response.json();
    renderHistory();
  } catch {
    /* history pane just keeps whatever it already had */
  }
}

function renderHistory() {
  const list = el("history-list");
  list.innerHTML = "";
  if (historyRows.length === 0) {
    const empty = document.createElement("p");
    empty.className = "wallet-note";
    empty.textContent = t("wallet.history_empty");
    list.appendChild(empty);
    return;
  }
  const filtered = historyRows.filter((row) => {
    if (historyFilter === "won") return row.won;
    if (historyFilter === "lost") return !row.won;
    return true;
  });
  if (filtered.length === 0) {
    const empty = document.createElement("p");
    empty.className = "wallet-note";
    empty.textContent = t("wallet.history_filter_empty");
    list.appendChild(empty);
    return;
  }
  for (const row of filtered) {
    const line = document.createElement("div");
    line.className = row.won ? "history-row won" : "history-row";
    const outcome = row.won
      ? t("wallet.history_won", { amount: row.won_amount })
      : t("wallet.history_lost");
    const dot = document.createElement("span");
    dot.className = "history-dot";
    const roundLabel = document.createElement("span");
    roundLabel.className = "history-main";
    roundLabel.textContent = t("wallet.history_round", { seq: row.seq, stake: row.stake });
    const outcomeLabel = document.createElement("span");
    outcomeLabel.className = "history-meta";
    outcomeLabel.textContent = outcome;
    line.appendChild(dot);
    line.appendChild(roundLabel);
    line.appendChild(outcomeLabel);
    list.appendChild(line);
  }
}

document.querySelectorAll("#history-filter-chips .amount-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll("#history-filter-chips .amount-chip").forEach((c) => c.classList.remove("selected"));
    chip.classList.add("selected");
    historyFilter = chip.dataset.filter;
    renderHistory();
  });
});

// --- toasts / errors -------------------------------------------------

function showToast(messageOrKey) {
  const toast = el("toast");
  // A translation key never contains a space ("error.generic"); an
  // already-resolved message might still contain a "." (a sentence's full
  // stop) without being one, so check for both rather than "." alone.
  const looksLikeKey = messageOrKey.includes(".") && !messageOrKey.includes(" ");
  toast.textContent = looksLikeKey ? t(messageOrKey) : messageOrKey;
  toast.classList.add("visible");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => toast.classList.remove("visible"), 2500);
}

ws.on("error", (msg) => showToast(msg.message_am || msg.code));

// A deposit (or any other out-of-round balance change) pushes this over the
// user's own `user:{id}` channel (services/payments/deposits.py) -- the
// header balance and, if it's currently open, the wallet screen both pick
// it up live instead of waiting for the player to reopen the wallet tab.
ws.on("balance_update", (msg) => {
  const state = getState();
  if (state.user) setState({ user: { ...state.user, balance: msg.cash } });
  if (getState().screen === "wallet") {
    el("wallet-cash").textContent = `${msg.cash} ETB`;
    el("wallet-bonus").textContent = `${msg.bonus} ETB`;
    el("wallet-locked").textContent = `${msg.locked} ETB`;
  }
  if (pendingDeposit && Number(msg.cash) - pendingDeposit.cashBefore >= pendingDeposit.amount - 0.01) {
    setWalletStatus("deposit-status", "wallet.deposit_confirmed", "success");
    pendingDeposit = null;
  }
});

// --- session-time reminders (spec section 12) -----------------------------
// "You've been playing 60 minutes" -- a plain awareness nudge, not an
// enforcement control (self-exclusion/cool-off/limits are already
// server-enforced -- see packages/core/responsible_gaming.py). Purely
// client-side and resets on reload by design, the same "this session"
// framing the reality check above uses.

const SESSION_REMINDER_MINUTES = [60, 120, 180];

function checkSessionReminder() {
  const state = getState();
  const elapsedMinutes = Math.floor((Date.now() - state.sessionStartedAt) / 60000);
  for (const threshold of SESSION_REMINDER_MINUTES) {
    if (elapsedMinutes >= threshold && !state.sessionRemindersShown.has(threshold)) {
      state.sessionRemindersShown.add(threshold);
      showToast(t("session.reminder", { minutes: threshold }));
      haptics.warning();
    }
  }
}

setInterval(checkSessionReminder, 60000);

// --- Telegram back button -------------------------------------------------

if (tg) {
  tg.BackButton.onClick(() => {
    const state = getState();
    if (state.screen === "wallet" || state.screen === "lobby") {
      showScreen("rooms");
    } else if (state.screen === "result") {
      showScreen("rooms");
    }
    // Deliberately not wired to close the app while a round is live
    // (spec section 3.5): only rooms/lobby/result respond to Back.
  });
}

// --- boot -----------------------------------------------------------------

async function boot() {
  if (tg) {
    tg.ready();
    tg.expand();
  }
  const preferredLanguage = tg && tg.initDataUnsafe && tg.initDataUnsafe.user
    ? tg.initDataUnsafe.user.language_code
    : "am";
  await initI18n(preferredLanguage);
  setLanguage(preferredLanguage);
  applyStaticTranslations();

  const initData = tg ? tg.initData : "";
  const hasInitData = Boolean(initData && initData.trim());
  if (!hasInitData) {
    // Show an explicit auth-failure shell so the player never sees a
    // featureless black screen if Telegram did not provide initData.
    const shell = el("boot-shell");
    shell.innerHTML = `
      <div class="boot-shell">
        <div class="boot-shell-title">${t("error.generic")}</div>
        <div class="boot-shell-body">${t("connection.connect_failed")}</div>
        <button class="boot-shell-action" data-action="retry">${t("connection.retry")}</button>
      </div>
    `;
    document.body.appendChild(shell);
    makeKeyboardActivatable(shell, () => window.location.reload());
    return;
  }

  ws.connect(initData);
  let user;
  try {
    user = await ws.waitForAuth();
  } catch {
    // A terminal auth failure (stale initData, most commonly) already
    // drives the "connection.expired" reload banner via the
    // connection-state subscriber above, independent of this promise --
    // nothing left to do here except stop, rather than hang forever
    // awaiting a user that ws.js now knows will never arrive.
    return;
  }
  setState({ user });
  // The language_code hint drove the boot screen above; now that the
  // server told us the real users.language, that value wins (spec 7.5).
  await applyServerLanguage(user.language);
  applyStaticTranslations();
  document.getElementById("balance-amount").textContent = `${user.balance} ETB`;
  showScreen("rooms");
  refreshRoomList();
}

subscribe((state) => {
  if (state.user) {
    document.getElementById("balance-amount").textContent = `${state.user.balance} ETB`;
  }
});

boot();
