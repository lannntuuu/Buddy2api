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

import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB_JS = Path(__file__).resolve().parent.parent / "src" / "web" / "js"
REPO_ROOT = WEB_JS.parent.parent.parent

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


# ---------------------------------------------------------------------------
# WS-3 静态 smoke(31 号方案 §5:零构建秒级防退化,断言关键实现仍在)
# ---------------------------------------------------------------------------

_API_JS = WEB_JS / "api.js"
_APP_JS = WEB_JS / "app.js"
_CSS = WEB_JS.parent / "css" / "app.css"


def _read(rel_or_abs) -> str:
    path = rel_or_abs if rel_or_abs.is_absolute() else (REPO_ROOT / rel_or_abs)
    return path.read_text(encoding="utf-8")


def test_error_text_map_covers_contract_statuses() -> None:
    """WS-3 §1:api.js 必须有 400/401/403/503 等状态错误文案映射表(含下一步动作)。"""
    src = _read(_API_JS)
    assert "export const ERR_TEXTS" in src, "missing ERR_TEXTS map in api.js"
    for status in ("400", "401", "403", "404", "429", "500", "502", "503", "504"):
        assert f"'{status}':" in src, f"ERR_TEXTS missing '{status}' entry"
    assert "export function apiErr" in src
    # 401 文案需引导到设置页(备用 Token 入口)
    match = re.search(r"'401':'([^']+)'", src)
    assert match, "cannot locate 401 entry in ERR_TEXTS"
    assert "设置" in match.group(1), "401 text should guide users to the settings page"


def test_api_get_dedup_and_timeout() -> None:
    """WS-3 §4:GET in-flight 去重 + 默认 15s 超时。"""
    src = _read(_API_JS)
    assert "INFLIGHT" in src, "GET in-flight dedupe map missing"
    assert "DEFAULT_TIMEOUT_MS=15000" in src.replace(" ", ""), "default 15s timeout missing"
    assert "AbortController" in src, "timeout must be implemented via AbortController"
    # 去重需含 token 维度,避免凭证变更后命中旧响应
    assert "'GET\\0'+(t||'')" in src or 'GET\\0' in src, "dedupe key should include token"


def test_index_html_defer_and_font_preload() -> None:
    """WS-3 §5:vendor script 加 defer;7 个 woff2 preload;模块加载顺序不破坏。"""
    html = _read(WEB_JS.parent / "index.html")
    assert '<script defer src="/static/vendor/vue.global.prod.min.js"></script>' in html
    assert '<script defer src="/static/vendor/Sortable.min.js"></script>' in html
    assert html.index("vue.global.prod.min.js") < html.index("Sortable.min.js") < html.index(
        '/static/js/app.js'
    ), "execution order Vue -> Sortable -> app.js must be preserved"
    fonts = [
        "Geist-Regular.woff2", "Geist-Medium.woff2", "Geist-SemiBold.woff2",
        "Geist-Bold.woff2", "GeistMono-Regular.woff2", "GeistMono-Medium.woff2",
        "GeistMono-SemiBold.woff2",
    ]
    for name in fonts:
        assert f'rel="preload" href="/static/fonts/{name}"' in html, f"missing preload for {name}"
        assert f'href="/static/fonts/{name}" as="font"' in html, f"preload of {name} must use as=font"
    assert html.count('as="font"') == 7
    # preload 需要 crossorigin(字体凭据模式),否则浏览器二次拉取
    for line in html.splitlines():
        if 'rel="preload"' in line:
            assert "crossorigin" in line, f"preload without crossorigin: {line.strip()}"


def test_font_display_swap_present() -> None:
    """WS-3 §5:font-display 确认,7 个 @font-face 全部声明 font-display:swap。"""
    css = _read(_CSS)
    assert css.count("font-display:swap") >= 7, "every @font-face must declare font-display:swap"


def test_toast_container_aria_and_sticky_error() -> None:
    """WS-3 §1:toast 容器 aria-live/role=status;错误 8s 长驻带关闭按钮;成功 2.5s。"""
    src = _read(_APP_JS)
    assert 'role="status"' in src and "aria-live=\"polite\"" in src.replace("'", '"'), \
        "toast container must be a polite live region"
    assert "TOAST_ERR_MS=8000" in src.replace(" ", ""), "error toast must be sticky (8s)"
    assert "TOAST_OK_MS=2500" in src.replace(" ", ""), "success toast must stay 2.5s"
    assert "toast-x" in src, "error toast needs a close button"
    # 401 动作按钮(去设置)接入
    assert "toastActionFor" in src and "toastGo" in src


def test_sortable_idempotent_guard_and_destroy() -> None:
    """WS-3 §4:Sortable 创建前先销毁旧实例(幂等守卫),onUnmounted destroy。"""
    src = _read(WEB_JS / "pages" / "channels.js")
    assert "function destroySortable" in src, "missing destroySortable helper"
    assert ".destroy()" in src
    assert "onUnmounted(destroySortable)" in src, "Sortable instances must be destroyed on unmount"
    guard_pos = src.index("destroySortable()")
    create_pos = src.index("Sortable.create")
    assert guard_pos < create_pos, "initSortable must destroy stale instances before creating"


def test_solo_poll_cleanup_on_unmount() -> None:
    """WS-3 §4:SOLO 登录轮询 onUnmounted 清理 + AbortController 中断在途请求。"""
    for name in ("channels.js", "_login_import.js"):
        src = _read(WEB_JS / "pages" / name)
        assert "onUnmounted" in src, f"{name}: missing onUnmounted cleanup"
        assert "AbortController" in src, f"{name}: in-flight poll must be abortable"
        assert "stopSoloPoll" in src, f"{name}: polling timer must be cleared"
        assert "soloGen++" in src, f"{name}: poll generation must be invalidated on unmount"


def test_keys_masked_with_reveal_endpoint() -> None:
    """WS-3 §7/契约 1:列表不再内联明文;掩码展示 + 查看按钮调 reveal 端点。"""
    src = _read(WEB_JS / "pages" / "keys.js")
    assert "/admin/api-keys/'+k.id+'/reveal" in src.replace('"', "'").replace(
        "/admin/api-keys/'+k.id+'/reveal", "/admin/api-keys/'+k.id+'/reveal"
    ) or "/reveal" in src, "reveal endpoint call missing"
    assert "revealed" in src, "revealed plaintext cache missing"
    assert "toggleReveal" in src and "cpKey" in src, "查看/复制 actions missing"
    assert "key_prefix" in src, "masked display must fall back to key_prefix"
    # 不允许再内联输出完整明文列表字段(k.key 已从契约中移除)
    assert "k.key?k.key" not in src, "legacy inline plaintext display must be removed"
    assert "toggleKey" not in src, "legacy toggleKey (plaintext from list) must be removed"
    # 接入指南页不得再从列表预填明文
    setup_src = _read(WEB_JS / "pages" / "setup.js")
    assert "k.key" not in setup_src, "setup page must not read plaintext key from list"


def test_row_writeback_helpers_and_fallback() -> None:
    """契约 6:respRow/respList/patchRowById 公共 helper 存在,四页消费且带整表回退。"""
    api_src = _read(_API_JS)
    for fn in ("export function respRow", "export function respList", "export function patchRowById"):
        assert fn in api_src, f"missing helper {fn}"
    consumers = {
        "pages/keys.js": ("respRow", "patchRowById"),
        "pages/channels.js": ("respRow", "patchRowById"),
        "pages/models.js": ("respList",),
        "pages/quota.js": ("respList",),
    }
    for rel, fns in consumers.items():
        src = _read(WEB_JS / "pages" / rel.split("/", 1)[1])
        for fn in fns:
            assert fn in src, f"{rel} must consume {fn}"
        # 每个消费点都要有整表 load 回退路径可用
        assert re.search(r"await (load|loadAll|loadAccounts)\(", src), \
            f"{rel}: missing full-load fallback for shape mismatch"


def test_quota_uses_batch_resources_endpoint() -> None:
    """契约 5:额度页批量刷新走 POST /admin/accounts/resources/batch,一次请求替代 N 个 GET。"""
    src = _read(WEB_JS / "pages" / "quota.js")
    assert "resources/batch" in src
    assert "account_ids" in src
    # 旧逐账号刷新保留为回退路径
    assert "refreshResource(" in src


def test_hash_routing_in_app() -> None:
    """WS-3 §6:go() 同步 location.hash,hashchange 驱动渲染,启动 hash 优先。"""
    src = _read(_APP_JS)
    assert "hashchange" in src, "missing hashchange listener"
    assert "location.hash" in src, "go() must sync location.hash"
    assert "pageFromHash" in src, "startup must prefer location.hash"
    # 空 hash 回退 localStorage 记忆
    assert "cb_gw_page_v2" in src


def test_keyboard_accessible_rows_and_rail() -> None:
    """WS-3 §2:rail 导航与通道行 tabindex=0 + Enter/Space 与 click 同义。"""
    app_src = _read(_APP_JS)
    assert 'tabindex="0"' in app_src, "rail items must be focusable"
    assert "@keydown.enter" in app_src and "@keydown.space.prevent" in app_src
    ch_src = _read(WEB_JS / "pages" / "channels.js")
    assert ch_src.count('tabindex="0"') >= 2, "both channel tables' rows must be focusable"
    assert "rowKey" in ch_src, "row keydown handler missing"
    # 行处理器需区分事件来源,避免行内按钮聚焦时 Enter 二次触发
    assert "e.target!==e.currentTarget" in ch_src.replace(" ", "")
    # focus-visible 焦点样式存在
    css = _read(_CSS)
    assert ":focus-visible" in css
