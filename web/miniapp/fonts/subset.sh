#!/usr/bin/env bash
# Regenerates NotoSansEthiopic-Regular.subset.woff2 from whatever Amharic
# strings currently exist in the locale files.
#
# Why this exists: Noto Sans Ethiopic's full "ethiopic" delivery from Google
# Fonts is ~200KB (the complete core Ethiopic Unicode block, hundreds of
# syllable glyphs) -- more than 4x the spec's 40KB budget for the Amharic
# font subset. Subsetting down to exactly the characters this UI actually
# uses gets it to ~32KB. The tradeoff: a new Amharic string introducing a
# character not yet in the subset will fall back to a system font for that
# glyph until this script is rerun. Run it after adding/changing Amharic
# copy in web/miniapp/locales/am.json or services/bot/locales/{am,ti}.json.
#
# Requires: fonttools (pip install fonttools brotli), curl.

set -euo pipefail
cd "$(dirname "$0")"

UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

echo "Fetching the current Noto Sans Ethiopic (regular) source from Google Fonts..."
CSS=$(curl -sL "https://fonts.googleapis.com/css2?family=Noto+Sans+Ethiopic:wght@400&display=swap" -A "$UA")
# -A8: the @font-face block is 9 lines long (comment, @font-face {,
# font-family, font-style, font-weight, font-stretch, font-display, src,
# unicode-range, }) -- the src: url(...) line the URL comes from is 7
# lines after the "/* ethiopic */" comment, not within -A2.
#
# -m1 (not `| head -1`): head closes the pipe as soon as it has its line,
# which under `set -o pipefail` can make the whole pipeline report failure
# from the SIGPIPE'd upstream grep even though the right output was
# already captured. grep -m1 stops itself instead, no SIGPIPE involved.
SRC_URL=$(echo "$CSS" | grep -A8 "/\* ethiopic \*/" | grep -oE -m1 "https://fonts.gstatic.com/[^)]+woff2")
curl -sL "$SRC_URL" -o /tmp/NotoSansEthiopic-source.woff2

echo "Collecting Unicode codepoints actually used in the locale files..."
CODEPOINTS=$(python3 - <<'EOF'
import json
import pathlib

files = [
    "../locales/am.json",
    "../../../services/bot/locales/am.json",
    "../../../services/bot/locales/ti.json",
]
chars = set()
for f in files:
    data = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
    for v in data.values():
        chars.update(v)
ethiopic = sorted(c for c in chars if 0x1200 <= ord(c) <= 0x139F)
print(",".join(f"U+{ord(c):04X}" for c in ethiopic))
EOF
)

echo "Subsetting to ${CODEPOINTS:0:60}... ($(echo "$CODEPOINTS" | tr ',' '\n' | wc -l) codepoints)"
pyftsubset /tmp/NotoSansEthiopic-source.woff2 \
  --output-file=NotoSansEthiopic-Regular.subset.woff2 \
  --flavor=woff2 \
  --unicodes="${CODEPOINTS},U+0020,U+002C" \
  --no-layout-closure \
  --no-hinting

ls -la NotoSansEthiopic-Regular.subset.woff2
