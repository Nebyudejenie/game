// Plain-node smoke test for web/miniapp/js/ws.js's reconnect backoff --
// there's no JS test framework anywhere in this repo (the Mini App is
// deliberately framework-free vanilla JS, see state.js's own docstring),
// so this uses only node's built-in assert module rather than adding one
// just for this. Invoked from tests/unit/test_miniapp_reconnect_backoff.py
// so it runs as part of the normal `pytest tests/` pass.

import assert from "node:assert/strict";
import { _reconnectDelayForAttempt } from "../../web/miniapp/js/ws.js";

// Attempt 0 must never exceed the base delay -- the very first retry
// after a drop should look like the old flat-1s behavior's ceiling, not
// something already longer.
for (let i = 0; i < 200; i++) {
  const d = _reconnectDelayForAttempt(0);
  assert.ok(d >= 0 && d <= 1000, `attempt 0 delay ${d} out of [0, 1000]`);
}

// The ceiling doubles with each failed attempt in a row.
assert.ok(_reconnectDelayForAttempt(1) <= 2000);
assert.ok(_reconnectDelayForAttempt(2) <= 4000);
assert.ok(_reconnectDelayForAttempt(3) <= 8000);

// A long losing streak must never exceed the 30s cap -- this is what
// actually stops the flat-retry-forever thundering-herd pattern the fix
// addresses, not just a slower-growing but still-unbounded backoff.
for (let i = 0; i < 50; i++) {
  const d = _reconnectDelayForAttempt(20);
  assert.ok(d >= 0 && d <= 30000, `attempt 20 delay ${d} exceeded the 30s cap`);
}

// Real jitter, not a disguised constant -- across many calls at the same
// attempt count, delays must vary. This is the actual point of the fix:
// decorrelating clients that all dropped at the same moment, not just
// giving every one of them a fixed longer wait.
const samples = new Set();
for (let i = 0; i < 20; i++) samples.add(_reconnectDelayForAttempt(5));
assert.ok(samples.size > 1, "delays for the same attempt count never varied -- no real jitter");

console.log("ok");
