// Plain-node smoke test for web/miniapp/js/amharic_numbers.js -- same
// no-framework discipline as test_reconnect_backoff.mjs. Invoked from
// tests/unit/test_amharic_number_mapping.py, which also cross-checks
// this module's own output (not a re-implementation of it) against
// packages/core/bingo.py's real letter_for() and the audio manifest.

import assert from "node:assert/strict";
import { AMHARIC_NUMBER_WORDS } from "../../web/miniapp/js/amharic_numbers.js";

// Every one of 1-75 must be present with a non-empty word -- no gaps.
for (let n = 1; n <= 75; n++) {
  const word = AMHARIC_NUMBER_WORDS[n];
  assert.ok(typeof word === "string" && word.length > 0, `number ${n} has no Amharic word`);
}
assert.equal(Object.keys(AMHARIC_NUMBER_WORDS).length, 75, "map must have exactly 75 entries");

// The five worked examples from the feature request itself, verbatim.
const expected = {
  7: "ሰባት",
  22: "ሃያ ሁለት",
  43: "አርባ ሦስት",
  56: "ሃምሳ ስድስት",
  74: "ሰባ አራት",
};
for (const [n, word] of Object.entries(expected)) {
  assert.equal(AMHARIC_NUMBER_WORDS[n], word, `number ${n}: expected "${word}", got "${AMHARIC_NUMBER_WORDS[n]}"`);
}

console.log("ok");
