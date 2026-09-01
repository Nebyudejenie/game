// Plain-node smoke test for web/miniapp/js/ws.js's waitForAuth() -- see
// test_reconnect_backoff.mjs for why this is a plain-node script.
// Invoked from tests/unit/test_miniapp_wait_for_auth_never_connects.py so
// it runs as part of the normal `pytest tests/` pass.
//
// A production incident (the Mini App opening to a permanently blank
// screen) traced to a gap test_wait_for_auth_terminal_failure.mjs's own
// fix doesn't cover: a WebSocket that never successfully opens at all
// (wrong URL, a reverse-proxy/tunnel not forwarding the Upgrade header,
// a firewall) rather than one that opens and then gets a terminal close
// code. That case has no close event with a terminal code for the
// existing fix to react to -- it just retries forever with backoff, and
// nothing ever settled waitForAuth()'s promise. This proves the new
// flat timeout closes that gap: a socket that just sits doing nothing
// (no open, no message, no close -- exactly what an unresponsive
// endpoint looks like from the client's side) must still make
// waitForAuth() reject within its own timeoutMs, not hang forever.

import assert from "node:assert/strict";

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this._listeners = {};
    FakeWebSocket.instances.push(this);
  }

  addEventListener(type, handler) {
    (this._listeners[type] ??= []).push(handler);
  }

  send() {}

  close() {
    this.readyState = FakeWebSocket.CLOSED;
  }
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSED = 3;
FakeWebSocket.instances = [];

globalThis.window = { location: { protocol: "https:", host: "test.local" } };
globalThis.WebSocket = FakeWebSocket;

const ws = await import("../../web/miniapp/js/ws.js");
const state = await import("../../web/miniapp/js/state.js");

class DeadlineExceeded extends Error {}

function withDeadline(promise, ms, label) {
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new DeadlineExceeded(`${label}: did not settle within ${ms}ms`)), ms);
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}

ws.connect("fake-init-data");
const socket = FakeWebSocket.instances[0];
assert.ok(socket, "connect() must open a WebSocket");
// Deliberately never fire "open", "message", or "close" on it -- this is
// exactly what an endpoint that never responds at all looks like from
// the client's side.

let rejected = false;
try {
  // A short override (well under the real 20s default) so this test
  // itself runs fast -- see waitForAuth()'s own comment for why this
  // parameter exists at all.
  await withDeadline(ws.waitForAuth(300), 2000, "waitForAuth() against a socket that never responds");
} catch (err) {
  assert.ok(!(err instanceof DeadlineExceeded), err.message);
  rejected = true;
}
assert.ok(rejected, "waitForAuth() must reject, not hang forever, when the socket never opens or authenticates");
assert.equal(
  state.getState().connection, "connect_failed",
  "a giving-up timeout must leave state in a distinct, banner-visible connect_failed state"
);

// connect()'s own open() started a real setInterval (the ping timer) that
// never got cleared -- the fake socket here never actually opens or
// closes, unlike test_wait_for_auth_terminal_failure.mjs's own script,
// where firing a real "close" event clears it as a side effect. Without
// this, node never exits on its own.
ws.disconnect();

console.log("ok");
