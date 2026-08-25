// Plain-node smoke test for web/miniapp/js/ws.js's _TERMINAL_CLOSE_CODES --
// see test_reconnect_backoff.mjs for why this is a plain-node script (no
// JS test framework anywhere in this repo) rather than something heavier.
// Invoked from tests/unit/test_miniapp_terminal_close_codes.py so it runs
// as part of the normal `pytest tests/` pass.

import assert from "node:assert/strict";
import { _TERMINAL_CLOSE_CODES } from "../../web/miniapp/js/ws.js";

// The exact three codes services/gateway/connection.py's own _handshake()
// closes with for a failure that will never succeed on retry: 4000
// (malformed/unexpected first frame), 4001 (no auth frame within the
// timeout), 4003 (initData rejected). A regression guard against this set
// silently drifting out of sync with the server's own codes -- either
// direction is a real bug: missing one means that failure mode goes back
// to retrying forever, and including a code the server doesn't actually
// use is harmless but meaningless.
assert.deepEqual(
  [..._TERMINAL_CLOSE_CODES].sort((a, b) => a - b),
  [4000, 4001, 4003]
);

// Ordinary transient/graceful codes must NOT be treated as terminal --
// these are exactly the cases that must still retry.
assert.ok(!_TERMINAL_CLOSE_CODES.has(1000), "1000 (normal closure) must not be terminal");
assert.ok(!_TERMINAL_CLOSE_CODES.has(1006), "1006 (abnormal closure) must not be terminal");
assert.ok(!_TERMINAL_CLOSE_CODES.has(1012), "1012 (service restart) must not be terminal");

console.log("ok");
