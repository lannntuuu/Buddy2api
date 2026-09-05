"""Encoding sanity check for the repository README pair.

Background: a historical bug double-encoded README.md and README_EN.md as
GBK, then a second pass converted ~3% of characters into PUA private-use
codepoints (U+E000-U+F8FF), making the docs unreadable. This file
guards against that class of regression with pure-stdlib assertions:
both READMEs must be strict UTF-8, no BOM, no PUA, no U+FFFD.

The minimum Chinese-character / line-count floors protect against a
"fix" that simply truncates the mojibake to silence the decoder — those
floors fail loudly if the docs are gutted instead of repaired.

Run via ``python -m pytest tests/test_docs_encoding.py -q``.
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
README_MD = REPO_ROOT / "README.md"
README_EN = REPO_ROOT / "README_EN.md"

BOM = b"\xef\xbb\xbf"


def _read_strict(path: pathlib.Path) -> bytes:
    data = path.read_bytes()
    data.decode("utf-8", errors="strict")  # raises on any non-UTF-8 byte
    return data


def _count(text: str, *, lo: int, hi: int) -> int:
    return sum(1 for ch in text if lo <= ord(ch) <= hi)


def test_readme_md_is_valid_utf8() -> None:
    data = _read_strict(README_MD)
    assert data[:3] != BOM, "README.md must not start with a UTF-8 BOM"


def test_readme_en_is_valid_utf8() -> None:
    data = _read_strict(README_EN)
    assert data[:3] != BOM, "README_EN.md must not start with a UTF-8 BOM"


def test_readme_md_no_pua_or_replacement() -> None:
    text = README_MD.read_text(encoding="utf-8")
    assert _count(text, lo=0xE000, hi=0xF8FF) == 0, "README.md contains PUA characters"
    assert "\ufffd" not in text, "README.md contains U+FFFD replacement chars"


def test_readme_en_no_pua_or_replacement() -> None:
    text = README_EN.read_text(encoding="utf-8")
    assert _count(text, lo=0xE000, hi=0xF8FF) == 0, "README_EN.md contains PUA characters"
    assert "\ufffd" not in text, "README_EN.md contains U+FFFD replacement chars"


def test_readme_md_uses_lf_line_endings() -> None:
    data = _read_strict(README_MD)
    assert b"\r" not in data, "README.md contains CR; expected LF line endings"


def test_readme_en_uses_lf_line_endings() -> None:
    data = _read_strict(README_EN)
    assert b"\r" not in data, "README_EN.md contains CR; expected LF line endings"


def test_readme_md_has_required_sections() -> None:
    text = README_MD.read_text(encoding="utf-8")
    for marker in ("# Buddy2api", "Bailian", "CB_BAILIAN_API_KEY", "CB_GATEWAY_PROVIDERS"):
        assert marker in text, f"README.md missing required marker: {marker!r}"


def test_readme_en_has_required_sections() -> None:
    text = README_EN.read_text(encoding="utf-8")
    for marker in ("# Buddy2api", "Bailian"):
        assert marker in text, f"README_EN.md missing required marker: {marker!r}"


def test_readme_md_chinese_floor() -> None:
    """Floor protects against a 'fix' that just strips the mojibake."""
    text = README_MD.read_text(encoding="utf-8")
    zh = _count(text, lo=0x4E00, hi=0x9FFF)
    assert zh >= 4000, f"README.md Chinese char count {zh} below floor 4000"


def test_readme_md_line_count_floor() -> None:
    text = README_MD.read_text(encoding="utf-8")
    n_lines = len(text.splitlines())
    assert n_lines >= 400, f"README.md line count {n_lines} below floor 400"
