"""One-off: mechanically revert README GBK-double-encoded mojibake.

Archive purpose: when both ``README.md`` and ``README_EN.md`` were
double-encoded (UTF-8 text decoded as GBK then re-encoded as UTF-8),
about 96.7% of the Chinese characters can be recovered by reversing the
operation per character::

    ch -> ch.encode('gbk') -> bytes.decode('utf-8')

A small fraction of characters (~3.3%, mostly PUA private-use area
codepoints U+E000-U+F8FF where the original GBK bytes were lost)
cannot be reversed; the script marks those positions with a ``「?」``
placeholder so the human rebuild pass can fill in context-appropriate
text.

Outputs drafts to ``.tmp/README.reverted.md`` and
``.tmp/README_EN.reverted.md``. ``.tmp/`` is gitignored so the drafts
never enter version control.

Do NOT import this from business code. Run from the repo root::

    python ops/scripts/oneoff/revert_readme_mojibake.py
"""
from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SOURCES = ("README.md", "README_EN.md")
PLACEHOLDER = "「?」"


def revert_mojibake(text: str) -> tuple[str, int, int]:
    """Reverse the GBK double-encoding on a string.

    Walks the input character by character. For each character that
    encodes cleanly as GBK, the encoded bytes are appended to a buffer;
    a failed encode (PUA or anything else outside the GBK range) inserts
    a ``「?」`` placeholder AND flushes the buffered bytes through
    ``decode('utf-8', errors='replace')`` so the placeholder sits in
    clean UTF-8 text. Returns the reverted text plus counts of
    successful and failed characters.
    """
    out: list[str] = []
    buf = bytearray()
    ok = 0
    bad = 0

    def flush() -> None:
        if buf:
            out.append(buf.decode("utf-8", errors="replace"))
            buf.clear()

    for ch in text:
        try:
            buf.extend(ch.encode("gbk"))
            ok += 1
        except (UnicodeEncodeError, OverflowError):
            bad += 1
            flush()
            out.append(PLACEHOLDER)
    flush()
    return "".join(out), ok, bad


def main() -> int:
    out_dir = REPO_ROOT / ".tmp"
    out_dir.mkdir(exist_ok=True)
    for name in SOURCES:
        src = REPO_ROOT / name
        text = src.read_text(encoding="utf-8")
        reverted, ok, bad = revert_mojibake(text)
        # Drafts are pure working artifacts; never written into git.
        dst = out_dir / name.replace(".md", ".reverted.md")
        dst.write_text(reverted, encoding="utf-8")
        total = ok + bad
        pct = (bad / total * 100.0) if total else 0.0
        print(
            f"{name}: chars={total} ok={ok} bad={bad} ({pct:.2f}%) -> {dst.relative_to(REPO_ROOT)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
