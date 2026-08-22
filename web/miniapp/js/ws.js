// WebSocket client matching services/gateway's protocol exactly (spec
// section 6). One connection for the whole app lifetime; reconnection is
// transparent to callers -- re-authenticates and re-joins whatever room
// was active, and the server's state_sync reply is what actually restores
// the player's place, not anything remembered client-side (spec principle
// 7: "A player's session can die at any moment. Reconnection must restore
// full state from the server in one message.")

import { setState, getState } from "./state.js";

const RECONNECT_DELAY_MS = 1000;
const PING_INTERVAL_MS = 20000;

let socket = null;
let initData = null;
let pingTimer = null;
let reconnectTimer = null;
let authResolvers = [];
const messageHandlers = new Map(); // type -> Set<fn>

function wsUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws`;
}

export function on(type, handler) {
  if (!messageHandlers.has(type)) messageHandlers.set(type, new Set());
  messageHandlers.get(type).add(handler);
  return () => messageHandlers.get(type).delete(handler);
}

function dispatch(message) {
  if (typeof message.server_time === "number") {
    setState({ serverTimeOffsetMs: message.server_time - Date.now() });
  }
  const handlers = messageHandlers.get(message.t);
  if (handlers) for (const handler of handlers) handler(message);
}

export function connect(rawInitData) {
  initData = rawInitData;
  open();
}

function open() {
  setState({ connection: getState().connection === "connected" ? "reconnecting" : "connecting" });
  socket = new WebSocket(wsUrl());

  socket.addEventListener("open", () => {
    send({ t: "auth", init_data: initData });
  });

  socket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.t === "authed") {
      setState({ connection: "connected", user: message.user });
      for (const resolve of authResolvers.splice(0)) resolve(message);
      const { currentRoomId } = getState();
      if (currentRoomId !== null) send({ t: "join", room_id: currentRoomId });
    }
    dispatch(message);
  });

  socket.addEventListener("close", (event) => {
    setState({ connection: "offline" });
    clearInterval(pingTimer);
    // 1012 = "service restart" (our own gateway's graceful-shutdown code,
    // spec section 6): reconnect immediately, no backoff needed.
    const delay = event.code === 1012 ? 0 : RECONNECT_DELAY_MS;
    reconnectTimer = setTimeout(open, delay);
  });

  socket.addEventListener("error", () => {
    socket.close();
  });

  pingTimer = setInterval(() => {
    if (socket && socket.readyState === WebSocket.OPEN) send({ t: "ping", ts: Date.now() });
  }, PING_INTERVAL_MS);
}

export function disconnect() {
  clearTimeout(reconnectTimer);
  clearInterval(pingTimer);
  if (socket) socket.close();
}

function send(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));
  }
}

export function waitForAuth() {
  if (getState().connection === "connected") return Promise.resolve(getState().user);
  return new Promise((resolve) => authResolvers.push(resolve));
}

export function requestRooms() {
  send({ t: "rooms" });
}

export function joinRoom(roomId) {
  setState({ currentRoomId: roomId });
  send({ t: "join", room_id: roomId });
}

export function leaveRoom(roomId) {
  send({ t: "leave", room_id: roomId });
  setState({ currentRoomId: null });
}

export function takeCard(roomId, cardNo) {
  send({ t: "take_card", room_id: roomId, card_no: cardNo, idem: `${roomId}-${cardNo}-${Date.now()}` });
}

export function dropCard(roomId) {
  send({ t: "drop_card", room_id: roomId });
}

export function setAuto(roomId, auto) {
  send({ t: "set_auto", room_id: roomId, auto });
}

export function claim(roundId) {
  send({ t: "claim", round_id: roundId });
}

export function mark(roundId, r, c) {
  send({ t: "mark", round_id: roundId, r, c });
}
