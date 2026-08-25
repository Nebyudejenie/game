"""Runs tests/frontend/test_terminal_close_codes.mjs -- a plain-node smoke
test for web/miniapp/js/ws.js's _TERMINAL_CLOSE_CODES, since there's no JS
test framework anywhere in this repo (the Mini App is deliberately
framework-free vanilla JS) and this fix is client-side reconnect logic
with nothing for the Python integration/unit suites to exercise directly.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "frontend" / "test_terminal_close_codes.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_terminal_close_codes_match_the_gateways_own_auth_failure_codes():
    result = subprocess.run(
        ["node", str(SCRIPT)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
