"""Backstop guard against prompt-injection text in the codebase.

After a planted-injection incident (a hidden "ignore-and-conceal"
instruction that briefly landed in a source file before being caught
on read-back), this test scans every tracked source file for
high-signal injection phrases and fails the build if any appear.

The marker list lives here; this file and the matching git hook are
excluded from the scan so the patterns themselves don't trip it.
"""

from __future__ import annotations

import re
from pathlib import Path

# repo root = .../app/tests/<thisfile> -> parents[2]
_REPO = Path(__file__).resolve().parents[2]

# High-signal phrases — deliberately specific so ordinary code and
# docs don't false-positive. A backstop, not a perfect classifier.
_MARKERS = [
    r"don'?t mention this",
    r"do not mention this",
    r"ignore (all )?(the )?previous instructions",
    r"disregard (all |the )?(the )?(above|previous)",
    r"append a section to your",
    r"do not tell the user",
    r"without (mentioning|telling) (this|it|the user)",
]
_RX = re.compile("|".join(_MARKERS), re.IGNORECASE)

_SCAN_EXT = {
    ".py", ".html", ".md", ".txt", ".toml", ".yaml", ".yml", ".cfg", ".sh",
}
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".githooks"}
_SELF = Path(__file__).name


def _source_files():
    for p in _REPO.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in _SCAN_EXT:
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.name == _SELF:
            continue
        yield p


def test_no_prompt_injection_markers_in_source():
    offenders: list[str] = []
    for path in _source_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _RX.finditer(text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(
                f"{path.relative_to(_REPO)}:{line}: {m.group(0)!r}"
            )
    assert not offenders, (
        "Possible prompt-injection text found in source:\n"
        + "\n".join(offenders)
    )
