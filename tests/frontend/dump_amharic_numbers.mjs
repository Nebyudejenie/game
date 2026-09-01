// Prints web/miniapp/js/amharic_numbers.js's AMHARIC_NUMBER_WORDS map as
// JSON on stdout. Lets tests/unit/test_amharic_number_mapping.py read the
// real map from Python (via a node subprocess) instead of hand-copying a
// second implementation of the word-construction rule that could drift
// from the real one.

import { AMHARIC_NUMBER_WORDS } from "../../web/miniapp/js/amharic_numbers.js";

console.log(JSON.stringify(AMHARIC_NUMBER_WORDS));
