"""Smoke check that the SPA's ESM modules are syntactically valid.

Background: the W5 chart-palette refactor renamed heatStyle to heatClass in
dashboard.js but the function body lost a closing parenthesis. The SPA
failed to mount in every browser, but the Python test suite does not load
the web assets so pytest stayed green. This file catches that class of
regression with a Node-based ESM parse (no execution, no DOM).

Run via `python -m pytest tests/test_web_assets.py -q`. The check
requires `node` on PATH; if missing, the parse tests are skipped.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB_JS = Path(__file__).resolve().parent.parent / "web" / "js"
REPO_ROOT = WEB_JS.parent.parent

# Every ES module that the SPA loads from <script type="module"> in index.html
JS_FILES = sorted(
    str(p.relative_to(REPO_ROOT)).replace("\\", "/")
    for p in WEB_JS.rglob("*.js")
)


_NODE = shutil.which("node")
node_required = pytest.mark.skipif(
    _NODE is None,
    reason="node not on PATH; install Node 18+ to enable SPA syntax checks",
)


_PARSE_DRIVER = r"""
import { readFileSync } from 'node:fs';
import { argv } from 'node:process';
const path = argv[2];
try {
  const src = readFileSync(path, 'utf8');
  // Use Function constructor in module context to force a real ESM parse
  // without executing side effects. node 22's parser is up to ES2024.
  new (await import('node:vm')).SourceTextModule(src, { identifier: path });
  console.log('OK');
} catch (e) {
  console.log('BAD ' + (e?.message || String(e)));
  process.exit(1);
}
"""


@node_required
@pytest.mark.parametrize("rel", JS_FILES)
def test_js_module_parses(rel: str) -> None:
    path = REPO_ROOT / rel
    driver = REPO_ROOT / ".tmp" / "_js_parse_check.mjs"
    driver.parent.mkdir(parents=True, exist_ok=True)
    driver.write_text(_PARSE_DRIVER, encoding="utf-8")
    try:
        # node:vm SourceTextModule needs --experimental-vm-modules
        result = subprocess.run(
            [_NODE, "--experimental-vm-modules", str(driver), str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        # Leave the driver file in place for fast re-runs (gitignored via .tmp).
        pass
    if result.returncode != 0 or "BAD" in result.stdout:
        msg = (result.stdout or result.stderr or "").strip().splitlines()[-1] if (result.stdout or result.stderr) else "node parse failed"
        pytest.fail(f"{rel} failed ESM parse: {msg}")


def test_vendor_present() -> None:
    """W1 vendored Vue and SortableJS under web/vendor. Fail loudly if missing."""
    vendor = WEB_JS.parent / "vendor"
    expected = ["vue.global.prod.min.js", "Sortable.min.js"]
    for name in expected:
        path = vendor / name
        assert path.is_file(), f"missing vendored asset: {path}"
        assert path.stat().st_size > 10_000, f"vendor file unexpectedly small: {path}"


def test_index_html_uses_vendored_scripts() -> None:
    """W1 replaced CDN script tags with /static/vendor/... — guard against regression."""
    html = (WEB_JS.parent / "index.html").read_text(encoding="utf-8")
    assert "/static/vendor/vue.global.prod.min.js" in html, "Vue not served from /static/vendor/"
    assert "/static/vendor/Sortable.min.js" in html, "Sortable not served from /static/vendor/"
    for forbidden in ("jsdelivr", "unpkg", "cdnjs"):
        assert forbidden not in html, f"CDN reference '{forbidden}' returned to index.html"
