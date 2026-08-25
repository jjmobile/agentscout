"""The only module allowed to build outgoing Technocore text (and the digest preview).

Every message: one physical line, swept like the server sweeps, bounded in length, and
always ending with the non-endorsement sentence. No call site can omit it.
"""
from __future__ import annotations

import unicodedata
from typing import List

DISCLAIMER = "Observed behaviour, not endorsement."
DEFAULT_MAX_CHARS = 1800
HARD_MAX_CHARS = 3800  # comfortably below Technocore's 4096
SEP = " | "


def sweep(text: str) -> str:
    """Mirror Technocore's single-line sweep: every invisible character becomes a space."""
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Zl", "Zp") or ch in "​‌‍⁠﻿":
            out.append(" ")
        else:
            out.append(ch)
    return " ".join("".join(out).split())


def sanitize_label(label: str, max_len: int = 24) -> str:
    s = sweep(label).replace('"', "'").replace("|", "/")
    return s[:max_len]


def one_line(parts: List[str], max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Join parts with ' | ', drop trailing optional parts until it fits, always end with DISCLAIMER."""
    max_chars = min(max_chars, HARD_MAX_CHARS)
    clean = [sweep(p) for p in parts if p and sweep(p)]
    clean = [p for p in clean if p != DISCLAIMER]
    while True:
        line = SEP.join(clean + [DISCLAIMER])
        if len(line) <= max_chars or len(clean) <= 1:
            break
        clean.pop()
    if len(line) > max_chars:  # single oversized part: cut it, keep the disclaimer intact
        room = max_chars - len(SEP) - len(DISCLAIMER) - 1
        line = clean[0][: max(0, room)].rstrip() + "…" + SEP + DISCLAIMER
    assert line.endswith(DISCLAIMER)
    return line
