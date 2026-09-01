# Bingo call audio clips

This directory holds one pre-generated `.mp3` per Bingo number, played by
`web/miniapp/js/voice.js` when the engine calls that number. No `.mp3`
files are committed here (`.gitignore` excludes them) — this directory
ships only the naming/text contract so real audio can be dropped in
later, from any source, with zero code changes.

## Naming

`{LETTER}_{NN}.mp3` — the Bingo letter (English, uppercase), an
underscore, and the number zero-padded to two digits:

```
B_01.mp3 ... B_15.mp3
I_16.mp3 ... I_30.mp3
N_31.mp3 ... N_45.mp3
G_46.mp3 ... G_60.mp3
O_61.mp3 ... O_75.mp3
```

Requested by `voice.js` at `/audio/calls/{LETTER}_{NN}.mp3`, served
automatically by the gateway's existing static mount — no route changes
needed.

## What each clip should say

`MANIFEST.json` maps every filename to its exact script, e.g.
`"G_56": "G! ሃምሳ ስድስት!"` — **the letter spoken in English, a short
natural pause, then the number spoken in Amharic.** This is the file to
hand to a TTS vendor or a voice actor. It's generated from two real
sources, never hand-typed, so it can't drift from them:

- the Bingo letter ranges in `packages/core/bingo.py` (`letter_for()`)
- the Amharic number words in `web/miniapp/js/amharic_numbers.js`

`tests/unit/test_amharic_number_mapping.py` verifies all three (the JS
word map, `MANIFEST.json`, and `bingo.py`'s ranges) stay consistent with
each other. If either source ever changes, regenerate `MANIFEST.json`
with:

```bash
python3 -c "
import json, subprocess
from packages.core.bingo import letter_for
words = json.loads(subprocess.run(
    ['node', 'tests/frontend/dump_amharic_numbers.mjs'], capture_output=True, text=True, check=True
).stdout)
manifest = {f'{letter_for(n)}_{n:02d}': f'{letter_for(n)}! {words[str(n)]}!' for n in range(1, 76)}
json.dump(manifest, open('web/miniapp/audio/calls/MANIFEST.json', 'w'), ensure_ascii=False, indent=2, sort_keys=True)
"
```

## Voice direction

A professional Bingo caller: energetic but clear, not rushed. The
letter and number should sound like two distinct beats, not run
together.

## Until real clips exist

`voice.js` handles a missing file gracefully: it logs one console
warning per missing filename and moves on to the next queued call.
Nothing in the game depends on these files existing.
