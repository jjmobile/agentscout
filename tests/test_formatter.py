import re
from pathlib import Path

from agentscout.formatter import DISCLAIMER, one_line, sanitize_label, sweep


def test_sweep_replaces_controls_format_and_bidi():
    s = sweep("a\nb\tc​d‮e f")
    assert s == "a b c d e f"


def test_one_line_always_ends_with_disclaimer_and_fits():
    parts = ["HEAD"] + ["item %d " % i + "x" * 100 for i in range(40)]
    line = one_line(parts, max_chars=500)
    assert line.endswith(DISCLAIMER) and len(line) <= 500 and "\n" not in line
    assert line.count(DISCLAIMER) == 1


def test_one_line_cuts_single_oversized_part_but_keeps_disclaimer():
    line = one_line(["y" * 5000], max_chars=300)
    assert line.endswith(DISCLAIMER) and len(line) <= 300


def test_disclaimer_cannot_be_duplicated_or_dropped():
    line = one_line(["a", DISCLAIMER, "b"])
    assert line.count(DISCLAIMER) == 1 and line.endswith(DISCLAIMER)


def test_sanitize_label():
    # the zero-width space is swept to a real space, quotes/pipes are replaced, then cut to 24
    assert sanitize_label('Ev"il|name​' + "x" * 40) == "Ev'il/name…"           # cut at the word boundary
    assert sanitize_label("x" * 40) == "x" * 23 + "…"                            # no boundary: hard cut
    assert sanitize_label("short name") == "short name"


def test_no_other_module_builds_outgoing_text():
    """Only formatter.py may know the disclaimer string or join outgoing parts."""
    src = Path(__file__).resolve().parents[1] / "src" / "agentscout"
    for p in src.glob("*.py"):
        if p.name == "formatter.py":
            continue
        text = p.read_text()
        assert "not endorsement" not in text.casefold(), p.name
        assert not re.search(r"""['"] \| ['"]\.join""", text), p.name
