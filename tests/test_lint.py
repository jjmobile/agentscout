import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_no_undefined_names_or_syntax_errors():
    """ruff F821/E9 over src, scripts and tests — catches a missing import before it crashes at go-live."""
    ruff = shutil.which("ruff") or str(ROOT / ".venv" / "bin" / "ruff")
    if not Path(ruff).exists():
        pytest.skip("ruff not installed")
    res = subprocess.run([ruff, "check", "--select", "F821,F822,E9", "src", "scripts", "tests"], cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
