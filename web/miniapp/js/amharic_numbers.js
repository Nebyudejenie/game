// Amharic number words for 1-75 -- the single source of truth for both
// the voice-call audio manifest (web/miniapp/audio/calls/MANIFEST.json)
// and voice.js's own file-naming, so the word list is never duplicated.
//
// Standard Amharic numeral construction: units (1-10) are their own
// words; 11-19 are "አሥር" (ten) + unit; each multiple of ten (20/30/...
// /70) has its own word; the numbers between are "tens-word + unit".
// Verified directly against every one of this feature's own worked
// examples (7->ሰባት, 22->ሃያ ሁለት, 43->አርባ ሦስት, 56->ሃምሳ ስድስት, 74->ሰባ አራት)
// before writing the rest by the same rule -- flagged here for a real
// native-speaker sanity check before treating as production-final, the
// same discipline this codebase's own am.json translations already
// follow (see DECISIONS.md).

const UNITS = [
  null, "አንድ", "ሁለት", "ሦስት", "አራት", "አምስት", "ስድስት", "ሰባት", "ስምንት", "ዘጠኝ",
];

const TENS = {
  10: "አሥር",
  20: "ሃያ",
  30: "ሰላሳ",
  40: "አርባ",
  50: "ሃምሳ",
  60: "ስድሳ",
  70: "ሰባ",
};

function wordFor(n) {
  if (n < 1 || n > 75) throw new RangeError(`${n} is not a valid bingo number (must be 1-75)`);
  if (n <= 9) return UNITS[n];
  if (n === 10) return TENS[10];
  if (n < 20) return `${TENS[10]} ${UNITS[n - 10]}`;
  const tens = Math.floor(n / 10) * 10;
  const unit = n % 10;
  return unit === 0 ? TENS[tens] : `${TENS[tens]} ${UNITS[unit]}`;
}

export const AMHARIC_NUMBER_WORDS = Object.freeze(
  Object.fromEntries(Array.from({ length: 75 }, (_, i) => i + 1).map((n) => [n, wordFor(n)]))
);
