"""Ensure the large static order-entry page contains parseable JavaScript."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest


def test_distribution_inline_javascript_is_syntactically_valid(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("Node.js not installed")
    file = Path(__file__).resolve().parents[1] / "app" / "static" / "distribution.html"
    html = file.read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?:\\s[^>]*)?>(.*?)</script>", html, re.DOTALL)
    assert scripts
    for index, script in enumerate(scripts):
        if not script.strip():
            continue
        path = tmp_path / f"inline_{index}.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
