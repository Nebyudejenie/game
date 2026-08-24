import { initI18n, t, setLanguage } from "./i18n.js";
import { getState, setState, subscribe, serverNow } from "./state.js";
import * as ws from "./ws.js";
import * as haptics from "./haptics.js";
import * as board from "./render/board.js";
import * as card from "./render/card.js";

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
  } else if (state.connection === "reconnecting" || state.connection === "offline") {
    banner.textContent = t("connection.reconnecting");
    banner.classList.add("visible");
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
    const isPlaying = room.status === "running" || room.status === "lobby";
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
        <div class="countdown">${room.status === "running" ? t("rooms.playing", { seconds: "" }) : ""}</div>
      </div>
    `;
    card.addEventListener("click", () => enterRoom(room.room_id));
    list.appendChild(card);
    void isPlaying;
  }
}

ws.on("rooms", (msg) => {
  setState({ rooms: msg.rooms });
  if (getState().screen === "rooms") renderRoomList();
});

function refreshRoomList() {
  ws.requestRooms();
}

// --- entering a room: state_sync decides which screen to show -----------

function enterRoom(roomId) {
  hasClaimedThisRound = false;
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
    cell.addEventListener("click", () => {
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
  badge.classList.remove("show");
  void badge.offsetWidth; // restart the CSS animation
  badge.classList.add("show");
  haptics.mediumTap();

  pushRecentCall(msg);
  el("stat-call").textContent = `${msg.index}/75`;
  updateBingoButton(calledSoFar);
});

function pushRecentCall(msg) {
  const recent = el("recent-calls");
  const chip = document.createElement("span");
  chip.textContent = `${msg.letter}${msg.number}`;
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

el("auto-switch").addEventListener("click", () => {
  const state = getState();
  const next = !state.autoMark;
  setState({ autoMark: next });
  el("auto-switch").classList.toggle("on", next);
  if (state.currentRoomId !== null) ws.setAuto(state.currentRoomId, next);
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
  if (mine) {
    el("result-title").textContent = t("result.win_title");
    el("result-title").classList.add("win");
    el("result-amount").textContent = `+ ${mine.amount} ETB`;
    el("result-meta").textContent = t("result.card_row", { card: mine.card_no, pattern: mine.pattern });
    haptics.success();
  } else if ((msg.winners || []).length > 0) {
    const winner = msg.winners[0];
    el("result-title").textContent = "";
    el("result-amount").textContent = `${winner.amount} ETB`;
    el("result-meta").textContent = t("result.session_line", { sign: "-", amount: stake });
  } else {
    el("result-title").textContent = "";
    el("result-amount").textContent = t("result.no_winner");
    el("result-meta").textContent = "";
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
    setWalletStatus("deposit-status", "wallet.deposit_ready", "success");
  } catch {
    setWalletStatus("deposit-status", "wallet.error.generic", "error");
  } finally {
    el("deposit-submit-btn").disabled = false;
  }
});

// --- withdraw ----------------------------------------------------------

el("withdraw-submit-btn").addEventListener("click", async () => {
  const amount = el("withdraw-amount-input").value;
  const accountRef = el("withdraw-account-input").value.trim();
  const holderName = el("withdraw-name-input").value.trim();
  if (!amount || Number(amount) <= 0 || !accountRef || !holderName) {
    setWalletStatus("withdraw-status", "wallet.error.invalid_amount", "error");
    return;
  }
  el("withdraw-submit-btn").disabled = true;
  setWalletStatus("withdraw-status", "wallet.withdraw_submitting", null);
  try {
    const response = await fetch("/api/withdraw", {
      method: "POST",
      headers: { ...authHeader(), "Content-Type": "application/json" },
      body: JSON.stringify({ amount, account_ref: accountRef, holder_name: holderName }),
    });
    if (response.status === 503) {
      setWalletStatus("withdraw-status", "wallet.not_available", "error");
      return;
    }
    const data = await response.json();
    if (!response.ok) {
      setWalletStatus("withdraw-status", `wallet.error.${data.detail || "generic"}`, "error");
      return;
    }
    setWalletStatus(
      "withdraw-status",
      data.status === "approved" ? "wallet.withdraw_approved" : "wallet.withdraw_review",
      "success"
    );
  } catch {
    setWalletStatus("withdraw-status", "wallet.error.generic", "error");
  } finally {
    el("withdraw-submit-btn").disabled = false;
  }
});

// --- history -------------------------------------------------------------

async function loadHistory() {
  const list = el("history-list");
  try {
    const response = await fetch("/api/history", { headers: authHeader() });
    if (!response.ok) return;
    const rows = await response.json();
    list.innerHTML = "";
    if (rows.length === 0) {
      const empty = document.createElement("p");
      empty.className = "wallet-note";
      empty.textContent = t("wallet.history_empty");
      list.appendChild(empty);
      return;
    }
    for (const row of rows) {
      const line = document.createElement("div");
      line.className = "history-row";
      const outcome = row.won
        ? t("wallet.history_won", { amount: row.won_amount })
        : t("wallet.history_lost");
      const roundLabel = document.createElement("span");
      roundLabel.textContent = t("wallet.history_round", { seq: row.seq, stake: row.stake });
      const outcomeLabel = document.createElement("span");
      outcomeLabel.className = "history-meta";
      outcomeLabel.textContent = outcome;
      line.appendChild(roundLabel);
      line.appendChild(outcomeLabel);
      list.appendChild(line);
    }
  } catch {
    /* history pane just keeps whatever it already had */
  }
}

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
  ws.connect(initData);
  const user = await ws.waitForAuth();
  setState({ user });
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
