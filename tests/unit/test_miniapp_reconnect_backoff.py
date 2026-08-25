"""Runs tests/frontend/test_reconnect_backoff.mjs -- a plain-node smoke
test for web/miniapp/js/ws.js's reconnect backoff, since there's no JS
test framework anywhere in this repo (the Mini App is deliberately
framework-free vanilla JS) and this fix is client-side timing logic with
nothing for the Python integration/unit suites to exercise directly.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "frontend" / "test_reconnect_backoff.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_reconnect_backoff_grows_capped_and_jittered():
    result = subprocess.run(
        ["node", str(SCRIPT)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
