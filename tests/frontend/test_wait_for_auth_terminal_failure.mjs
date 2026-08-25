// Plain-node smoke test for web/miniapp/js/ws.js's waitForAuth() -- see
// test_reconnect_backoff.mjs for why this is a plain-node script (no JS
// test framework anywhere in this repo) rather than something heavier.
// Invoked from tests/unit/test_miniapp_wait_for_auth_terminal_failure.py so
// it runs as part of the normal `pytest tests/` pass.
//
// ws.js reaches into `window.location` and `WebSocket`, both browser
// globals Node doesn't have -- stubbed here with the minimum needed to
// drive connect()'s open()/close() event flow by hand, rather than
// pulling in a real browser or a fake-WebSocket package just for this.

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

  _fire(type, event) {
    for (const handler of this._listeners[type] || []) handler(event);
  }
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSED = 3;
FakeWebSocket.instances = [];

globalThis.window = { location: { protocol: "https:", host: "test.local" } };
globalThis.WebSocket = FakeWebSocket;

const ws = await import("../../web/miniapp/js/ws.js");

// A short internal deadline so a future regression that reintroduces the
// hang fails fast with a clear assertion, instead of this script (and the
// pytest subprocess wrapping it) just hanging until its own 30s timeout.
// Marked with its own class so callers can tell "the promise itself
// rejected" (the fix working) apart from "nothing ever happened" (the bug
// this test exists to catch) -- both land in the same catch block
// otherwise, which would let a reintroduced hang report as a pass.
class DeadlineExceeded extends Error {}

function withDeadline(promise, ms, label) {
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new DeadlineExceeded(`${label}: did not settle within ${ms}ms`)), ms);
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}

// A code review pass caught that a terminal auth failure (stale initData,
// most commonly -- see _TERMINAL_CLOSE_CODES' own comment in ws.js) never
// settled a still-pending waitForAuth() promise: "authed" is the only
// other place that ever resolved one, and that message is never coming
// once the socket closes with a terminal code. app.js's boot() awaits
// waitForAuth() directly, so this hung the entire Mini App forever on
// first load instead of ever reaching the reload banner the connection
// -state subscriber already shows independently.
ws.connect("fake-init-data");
const socket = FakeWebSocket.instances[0];
assert.ok(socket, "connect() must open a WebSocket");

const authPromise = ws.waitForAuth();
socket._fire("close", { code: 4003 }); // initData rejected -- a real terminal code

let rejected = false;
try {
  await withDeadline(authPromise, 2000, "waitForAuth() after a terminal close");
} catch (err) {
  assert.ok(!(err instanceof DeadlineExceeded), err.message);
  rejected = true;
}
assert.ok(rejected, "waitForAuth() must reject, not hang forever, once a terminal close fires");

// And a *second* caller, checking auth only after the failure already
// happened, must also reject immediately rather than queuing a resolver
// that would wait forever too -- no "authed" message is ever coming for
// this connection again.
let rejectedAgain = false;
try {
  await withDeadline(ws.waitForAuth(), 2000, "waitForAuth() called again after auth_failed");
} catch (err) {
  assert.ok(!(err instanceof DeadlineExceeded), err.message);
  rejectedAgain = true;
}
assert.ok(rejectedAgain, "waitForAuth() called again after auth_failed must also reject, not hang");

console.log("ok");
