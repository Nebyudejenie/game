// Minimal pub/sub state container. No framework needed for a state tree
// this small (spec: "avoid heavy frameworks; vanilla + a tiny reactive
// layer... is plenty").

const state = {
  connection: "connecting", // connecting | connected | reconnecting | offline
  user: null, // {id, name, balance}
  serverTimeOffsetMs: 0, // server_time - Date.now(), refreshed on every message that carries one
  screen: "rooms", // rooms | lobby | game | result | wallet
  rooms: [],
  currentRoomId: null,
  round: null, // last state_sync payload for the joined room
  yourCardGrid: null, // 5x5 grid for the card this user holds, once known
  autoMark: true,
  lockedOut: false,
  lastResult: null,
};

const listeners = new Set();

export function getState() {
  return state;
}

export function setState(patch) {
  Object.assign(state, patch);
  for (const listener of listeners) listener(state);
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function serverNow() {
  return Date.now() + state.serverTimeOffsetMs;
}
