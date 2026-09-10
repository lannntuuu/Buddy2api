"""Smoke check that the SPA's ESM modules parse *and link* cleanly.

Background: the W5 chart-palette refactor renamed heatStyle to heatClass in
dashboard.js but the function body lost a closing parenthesis. The SPA
failed to mount in every browser, but the Python test suite does not load
the web assets so pytest stayed green. This file catches that class of
regression with a Node-based ESM parse (no execution, no DOM).

Upgrade (spec 38 防线 1): parsing alone accepts a module that references a
symbol no module exports -- ESM resolves import/export bindings in the
*link* phase, not the parse phase. The driver below now runs
`SourceTextModule.link()` with a linker that resolves relative specifiers
against each file's own directory, so `import {nope} from './api.js'` fails
here instead of in the browser.

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
import { dirname, resolve as resolvePath } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { argv } from 'node:process';
const { SourceTextModule } = await import('node:vm');
const path = argv[2];

// Cache so a shared dependency (api.js under app.js + 10 pages) is only
// constructed once per node process.
const cache = new Map();

function makeModule(file) {
  const key = fileURLToPath(pathToFileURL(file));
  const hit = cache.get(key);
  if (hit) return hit;
  const mod = new SourceTextModule(readFileSync(file, 'utf8'), { identifier: key });
  cache.set(key, mod);
  return mod;
}

// Relative specifiers are resolved against the importing file's own directory:
// ./api.js and ./pages/dashboard.js from app.js, ../api.js from pages/*.js.
async function linker(specifier, referencingModule) {
  const target = resolvePath(dirname(referencingModule.identifier), specifier);
  return makeModule(target);
}

try {
  const mod = makeModule(path);
  // link() resolves every import/export binding: a name that the target
  // module does not export throws here (parse alone would not catch it).
  await mod.link(linker);
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
    """WS-3 §4:SOLO 登录轮询 onUnmounted 清理 + AbortController 中断在途请求。

    断言修订声明(spec 34 §WS-B-3,全 spec 唯一允许的既有断言改动):
    channels.js 的 scan/solo 死码已删除(33 号方案 §2-E;导入 UI 早已迁至
    _login_import.js,channels 模板零引用),SOLO 轮询清理由 _login_import.js
    独立承担,故本断言从文件元组收窄为仅 _login_import.js。
    """
    src = _read(WEB_JS / "pages" / "_login_import.js")
    assert "onUnmounted" in src, "missing onUnmounted cleanup"
    assert "AbortController" in src, "in-flight poll must be abortable"
    assert "stopSoloPoll" in src, "polling timer must be cleared"
    assert "soloGen++" in src, "poll generation must be invalidated on unmount"


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


# ---------------------------------------------------------------------------
# WS-B 安全网(33 号方案 §4-2):CSS 单向 smoke
# 断言「模板+JS 用到的类名全集 ⊆ app.css 选择器类名全集」。单向:只保证
# 用到的类都有样式(或已列入白名单),不反向要求 CSS 无冗余(死块另行清理)。
# ---------------------------------------------------------------------------

# JS 里动态拼进 :class 的类名(非模板字面量,无法静态提取)单独枚举保护:
# heatClass(dashboard)/cacheStatusClass(dashboard)/statusClass(logs)/
# checkinClass+pkgBadge(quota)/healthClass(dashboard)/toast 类型(app.js)/
# today-metric 与 today-hour-chart 的 kind 类。
JS_DYNAMIC_CLASSES = {
    "ok", "err", "warn", "info", "active", "inactive",
    "requests", "tokens", "credit",
    "heat-0", "heat-1", "heat-2", "heat-3", "heat-4", "heat-5",
    "cache-ok", "cache-partial", "cache-approx", "cache-empty",
}

# 模板中出现但 app.css 无选择器的类名(现状基线,一次性脚本核实):
# 样式由内联 style 承担,或仅作为 SortableJS 钩子/纯语义标记。
#   apikey      通道列表 tag 语义标记(.tag 提供样式)
#   drag-handle 拖拽手柄(样式全内联;SortableJS handle 钩子)
#   grp-h       分组表头行(样式全内联;SortableJS onMove 排除钩子)
#   chan        凭证表分组头标记(与 grp-h 共用,纯语义;样式全内联)
#   sec-h       channels 小节标题(无专用样式,默认排版)
#   tab/tabbar  详情浮窗 tab 行(无专用样式,.on 态由 .seg button.on 等承担)
#   ch-warn     env 锁定提示(原 .ch-panel .ch-warn 因页面无 .ch-panel 祖先从未
#               生效,WS-B 死块删除后该类只剩语义标记作用,样式由内联 style 承担)
NO_CSS_WHITELIST = {"apikey", "drag-handle", "grp-h", "chan", "sec-h", "tab", "tabbar", "ch-warn"}


def test_css_covers_all_used_classes() -> None:
    """模板静态类名 + :class 字面量 + JS 动态类名 ⊆ app.css 选择器类名。"""
    css = _read(_CSS)
    css_classes = set(re.findall(r"\.([a-zA-Z][a-zA-Z0-9_-]*)", css))
    used = set(JS_DYNAMIC_CLASSES)
    for rel in JS_FILES:
        src = _read(REPO_ROOT / rel)
        # 模板字符串:剔除 ${...} 插值(动态部分走 JS_DYNAMIC_CLASSES)
        for tmpl in re.findall(r"`(?:[^`\\]|\\.)*`", src, re.S):
            static = re.sub(r"\$\{.*?\}", " ", tmpl)
            for m in re.finditer(r'(?<![:\w])class="([^"]*)"', static):
                used.update(m.group(1).split())
            for m in re.finditer(r':class="([^"]*)"', static):
                seg = m.group(1)
                # 值位置(三元 ?/: 分支、数组元素)的引号串;比较操作数(x==='y')不算
                used.update(re.findall(r"""(?<=[?:,(\[])\s*['"]([a-zA-Z][a-zA-Z0-9_-]*)['"]""", seg))
                # 对象键 {on:...} 无引号,键即类名
                used.update(re.findall(r"[{,\s]([a-zA-Z][a-zA-Z0-9_-]*):", seg))
    missing = sorted(c for c in used if c not in css_classes and c not in NO_CSS_WHITELIST)
    assert not missing, f"classes used in templates/JS but missing in app.css: {missing}"

def test_shared_channels_store():
    """WS-E:跨页通道下拉单飞缓存 + 通道健康徽标消费。"""
    app_js = _read(Path("src/web/js") / "app.js")
    assert "ensureChannels" in app_js and "sharedChannels" in app_js
    assert "invalidateChannels" in app_js
    usage_js = _read(Path("src/web/js/pages") / "usage.js")
    assert "ensureChannels" in usage_js
    keys_js = _read(Path("src/web/js/pages") / "keys.js")
    assert "ensureChannels" in keys_js
    channels_js = _read(Path("src/web/js/pages") / "channels.js")
    assert "/admin/channel-health" in channels_js
    assert "healthClass" in channels_js and "health-dot" in channels_js


# ---------------------------------------------------------------------------
# 父子组件 props 契约(回归护栏)
# 事故:app.js 只在根 setup 的 return 里挂了 ensureChannels,模板没传,子页
# props 也没声明 → `p.ensureChannels is not a function` 让 keys 页 load()
# 抛错(列表空 + toast「加载失败」)、usage 页平台下拉空。下面两条断言把
# 「子 setup 里 p.X 的每个 X 都必须在 props 里声明」和「模板必须传」钉死。
# ---------------------------------------------------------------------------

# 根模板里每个页面组件的挂载点,以及需要由父级注入的 props(kebab 形式)
_PAGE_FILES = {
    "dash": WEB_JS / "pages" / "dashboard.js",
    "chns": WEB_JS / "pages" / "channels.js",
    "mdls": WEB_JS / "pages" / "models.js",
    "quota": WEB_JS / "pages" / "quota.js",
    "keys": WEB_JS / "pages" / "keys.js",
    "usg": WEB_JS / "pages" / "usage.js",
    "lgs": WEB_JS / "pages" / "logs.js",
    "setup": WEB_JS / "pages" / "setup.js",
    "stgs": WEB_JS / "pages" / "settings.js",
}
_PAGE_PROP_CONTRACT = {
    "dash": {},
    "chns": {"invalidate-channels"},
    "mdls": {},
    "quota": {},
    "keys": {"ensure-channels"},
    "usg": {"ensure-channels"},
    "lgs": {},
    "setup": {},
    "stgs": {},
}


def _camel(kebab: str) -> str:
    head, *rest = kebab.split("-")
    return head + "".join(part.title() for part in rest)


def test_page_components_declare_injected_props() -> None:
    """子 setup 里 p.X 的每个 X 必须在该组件的 props 数组中声明。"""
    for tag, injected in _PAGE_PROP_CONTRACT.items():
        src = _read(_PAGE_FILES[tag])
        declared = set(re.search(r"props:\[([^\]]*)\]", src).group(1).replace("'", "").replace('"', "").replace(" ", "").split(",")) - {""}
        for kebab in injected:
            assert _camel(kebab) in declared, (
                f"{tag} uses p.{_camel(kebab)} but does not declare it in props: {sorted(declared)}"
            )


def test_root_template_passes_injected_props() -> None:
    """根模板必须显式绑定每个由父级注入的 prop(挂在 return 里不算)。"""
    app_js = _read(_APP_JS)
    for tag, injected in _PAGE_PROP_CONTRACT.items():
        mount = re.search(r"<" + tag + r"\b[^>]*>", app_js)
        assert mount, f"root template missing mount point for <{tag}>"
        for kebab in injected:
            assert f":{kebab}=" in mount.group(0), f"<{tag}> must bind :{kebab}="


# ---------------------------------------------------------------------------
# spec 38 防线 2:静态绑定检查(抓「用了共享符号却忘了 import」)
# 事故:app.js 里 `api.get(...)` 是未声明的自由标识符(L2 只导入了
# toastActionFor),ESM 严格模式下读取未声明标识符抛 ReferenceError,
# ensureChannels 第一行就炸 → catch 吞掉 → API Keys 列表空 + toast
# 「加载失败」+ 通道下拉空。ESM 解析期对此完全合法(防线 1 的 link 也
# 抓不到,因为缺陷是"缺 import"而非"import 了不存在的导出名"),
# 故补这道正则静态检查。
#
# 这是启发式:只做模块符号绑定层面的判定,不做数据流/类型分析。
# 两个 mask 分工:
#   _strip_comments(text, keep_strings=True)  -> 结构面:import/export/声明仍可读
#   _strip_comments(text, keep_strings=False) -> 引用面:只剩真实代码位置,
#       字符串与模板纯文本被抹掉(避免模板/提示文案里的 "api." 之类误报),
#       模板的 ${...} 插值按原样保留(那是真 JS 作用域)。
# ---------------------------------------------------------------------------

# 跨页共享符号的出处:导出名由这些文件的 export 声明动态提取,不硬编码。
_SHARED_MODULES = ["api.js", "format.js", "icons.js"]

_ID = r"[A-Za-z_$][A-Za-z0-9_$]*"

# 「当作值引用」的判定:标识符后面紧跟这些字符说明它在表达式/调用位置,
# 而不是对象字面量的键、属性名、声明名等。
_VALUE_NEXT = set("([,.)];+-*/%?!&|<>~^=")


def _strip_comments(src: str, keep_strings: bool = False) -> str:
    """抹掉 // 与 /* */ 注释;可选再把字符串/模板正文抹成空格。

    返回与 src 等长的字符串(换行位置保留),因此可以据此回算行号。
    """
    out: list[str] = []
    i, n = 0, len(src)
    prev_sig = ""  # 上一个有意义的字符,用于判别正则字面量

    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            out.append("  ")
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
            continue
        # keep_strings 时不做引号/正则处理:import ... from 'x' 需要保留字符串
        if keep_strings:
            out.append(c)
            if not c.isspace():
                prev_sig = c
            i += 1
            continue
        # 正则字面量(启发式:前一个有效字符只允许出现在值位置)
        if c == "/" and prev_sig in "(,=:[!&|?{};+-*%~^<>":
            j = i + 1
            in_cls = False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "\n":
                    break
                if in_cls:
                    if ch == "]":
                        in_cls = False
                elif ch == "[":
                    in_cls = True
                elif ch == "/":
                    j += 1
                    break
                j += 1
            out.append(" " * (j - i))
            i = j
            prev_sig = "/"
            continue
        if c in "\"'":
            q = c
            out.append(" ")
            i += 1
            while i < n:
                if src[i] == "\\":
                    out.append("  ")
                    i += 2
                    continue
                if src[i] == q or src[i] == "\n":
                    out.append(" ")
                    i += 1
                    break
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            prev_sig = q
            continue
        if c == "`":
            out.append(" ")
            i += 1
            while i < n:
                if src[i] == "\\":
                    out.append("  ")
                    i += 2
                    continue
                if src[i] == "`":
                    out.append(" ")
                    i += 1
                    break
                if src[i] == "$" and i + 1 < n and src[i + 1] == "{":
                    out.append("  ")
                    i += 2
                    depth = 1
                    while i < n:
                        ch = src[i]
                        if ch in "\"'":
                            out.append(" ")
                            i += 1
                            while i < n and src[i] != ch:
                                out.append(" ")
                                i += 2 if src[i] == "\\" else 1
                            if i < n:
                                out.append(" ")
                                i += 1
                            continue
                        if ch == "`" or ch == "\n":
                            break  # 嵌套模板/换行:放弃这段插值,按文本处理
                        if ch == "{":
                            depth += 1
                        elif ch == "}":
                            depth -= 1
                            if depth == 0:
                                out.append(" ")
                                i += 1
                                break
                        out.append(ch)
                        i += 1
                    continue
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            prev_sig = "`"
            continue
        out.append(c)
        if not c.isspace():
            prev_sig = c
        i += 1
    return "".join(out)


def _balanced(code: str, start: int) -> str:
    """取 start 处 [/{ 起始的括号配对内容。"""
    open_ch, close_ch = code[start], ("]" if code[start] == "[" else "}")
    depth = 0
    for i in range(start, len(code)):
        if code[i] == open_ch:
            depth += 1
        elif code[i] == close_ch:
            depth -= 1
            if depth == 0:
                return code[start:i + 1]
    return code[start:]


def _import_bound_names(clause: str) -> list[str]:
    """`a, b as c` → 绑定名取别名:['a','c']。"""
    names = []
    for part in clause.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(rf"^({_ID})\s+as\s+({_ID})$", part)
        if m:  # X as Y —— 绑定名是 Y
            names.append(m.group(2))
        elif re.match(rf"^{_ID}$", part):
            names.append(part)
    return names


def _export_clause_names(clause: str) -> list[str]:
    """export {a, b as c} —— 对外名是 X(as 之前),故与 import 相反。"""
    names = []
    for part in clause.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(rf"^({_ID})\s+as\s+({_ID})$", part)
        names.append(m.group(1) if m else part if re.match(rf"^{_ID}$", part) else "")
    return [x for x in names if x]


def _exported_names(code: str) -> set[str]:
    """本文件对外导出的符号名(export const/function/class/{} /default)。"""
    names: set[str] = set()
    for m in re.finditer(rf"\bexport\s+(?:async\s+)?(?:const|let|var|function\s*\*?|class)\s+({_ID})", code):
        names.add(m.group(1))
    for m in re.finditer(r"\bexport\s+(?:const|let|var)\s*([\[{])", code):
        names.update(re.findall(_ID, _balanced(code, m.start(1))))
    for m in re.finditer(r"\bexport\s*\{([^{}]*)\}", code):
        names.update(_export_clause_names(m.group(1)))
    if re.search(r"\bexport\s+default\b", code):
        names.add("default")  # 页面组件的默认导出,不被当作缺失 import
    return names


def _bound_import_names(code: str) -> set[str]:
    """本文件由 import 绑定的名字:default / namespace / named(含 as 别名)。"""
    names: set[str] = set()
    for m in re.finditer(r"\bimport\b([^;]*?)\bfrom\b\s*['\"][^'\"]*['\"]", code, re.S):
        clause = m.group(1).strip()
        dm = re.match(rf"^({_ID})\s*(?:,|$)", clause)  # default import
        if dm:
            names.add(dm.group(1))
            clause = clause[dm.end():].lstrip(", ")
        for nm in re.finditer(rf"\*\s+as\s+({_ID})", clause):  # import * as X
            names.add(nm.group(1))
        for br in re.finditer(r"\{([^{}]*)\}", clause):
            names.update(_import_bound_names(br.group(1)))
    return names


def _skip_initializer(code: str, i: int) -> int:
    """跳过一个初始化表达式,停在顶层 , ; 或未配对的 )。"""
    depth = 0
    while i < len(code):
        c = code[i]
        if c in "\"'`":
            i += 1
            while i < len(code) and code[i] != c:
                i += 2 if code[i] == "\\" else 1
            i += 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return i
            depth -= 1
        elif c in ",;" and depth == 0:
            return i
        i += 1
    return i


def _declarators(code: str, start: int) -> set[str]:
    """const/let/var 声明符列表绑定的名字(含 `const a=...,b=...` 与解构)。"""
    names: set[str] = set()
    i, n = start, len(code)
    while i < n:
        while i < n and code[i].isspace():
            i += 1
        if i >= n:
            break
        if code[i] in "{[":
            body = _balanced(code, i)
            names.update(re.findall(_ID, body))
            i += len(body)
        else:
            m = re.match(rf"({_ID})", code[i:])
            if not m:
                break
            names.add(m.group(1))
            i += m.end()
        while i < n and code[i].isspace():
            i += 1
        if i < n and code[i] == "=":
            i = _skip_initializer(code, i + 1)
        if i < n and code[i] == ",":
            i += 1
            continue
        break
    return names


def _local_names(code: str) -> set[str]:
    """本文件的局部绑定:const/let/var/function/class/参数/catch 参数。

    覆盖度不必达到编译器级别:漏掉一个局部名只会多报一条,而这条断言
    在现行代码库上的基数是 0,任何新报告都会被人工判定(见 §5)。
    """
    names: set[str] = set()
    for m in re.finditer(r"\b(?:const|let|var)\s", code):
        names.update(_declarators(code, m.end()))
    for m in re.finditer(rf"\bfunction\s*\*?\s*({_ID})", code):
        names.add(m.group(1))
    for m in re.finditer(rf"\bclass\s+({_ID})", code):
        names.add(m.group(1))
    for pat in (rf"\bfunction\s*\*?\s*(?:{_ID}\s*)?\(([^()]*)\)", r"\(([^()]*)\)\s*=>"):
        for m in re.finditer(pat, code):
            names.update(re.findall(_ID, m.group(1)))
    for m in re.finditer(rf"(?:^|[^\w$.])({_ID})\s*=>", code, re.M):  # 单参数箭头函数
        names.add(m.group(1))
    for m in re.finditer(rf"\bcatch\s*\(\s*({_ID})\s*\)", code):
        names.add(m.group(1))
    return names


def _value_ref_lines(code: str, name: str) -> list[int]:
    """name 被当作值引用的行号列表(排除 obj.name 这类属性访问)。"""
    lines = []
    for m in re.finditer(rf"(?<![\w$.]){re.escape(name)}(?![\w$])", code):
        i = m.start()
        if i and code[i - 1] == ".":  # 属性访问 obj.api / props.api 不算
            continue
        nxt = code[m.end()] if m.end() < len(code) else ""
        if nxt in _VALUE_NEXT:
            lines.append(code[:i].count("\n") + 1)
    return lines


def _shared_export_names() -> set[str]:
    """动态提取 api.js/format.js/icons.js 的导出名(避免硬编码漂移)。"""
    names: set[str] = set()
    for mod in _SHARED_MODULES:
        src = _read(WEB_JS / mod)
        names |= _exported_names(_strip_comments(src, keep_strings=True))
    return names


def test_shared_export_scan_is_not_empty() -> None:
    """护栏:导出名提取必须真的抓到符号,否则绑定检查会空转(永远绿)。"""
    names = _shared_export_names()
    for mod in _SHARED_MODULES:
        assert _exported_names(_strip_comments(_read(WEB_JS / mod), keep_strings=True)), \
            f"derived no exports from {mod}; the regex no longer matches its export style"
    # 这两个名字是 38 号事故与别名用例的主角,缺任一个都说明提取坏了
    assert {"api", "withBusy", "fmtSec"} <= names, f"shared export derivation broke: {sorted(names)}"


def test_no_unimported_shared_symbol_reference() -> None:
    """每个被当作值引用的共享符号,必须已 import / 本文件导出 / 局部声明。

    对应缺陷:app.js 用 `api.get(...)` 却没 import api(spec 38 §0.2 缺陷 B)。
    """
    shared = _shared_export_names()
    problems: list[str] = []
    for rel in JS_FILES:
        raw = _read(REPO_ROOT / rel)
        struct = _strip_comments(raw, keep_strings=True)  # 结构面:识别 import/声明
        scan = _strip_comments(raw, keep_strings=False)   # 引用面:只剩真实代码
        bound = _bound_import_names(struct)
        own = _exported_names(struct)
        local = _local_names(struct)
        for name in sorted(shared):
            if name in bound or name in own or name in local:
                continue
            lines = _value_ref_lines(scan, name)
            if lines:
                where = ", ".join(f"L{n}" for n in lines[:5])
                problems.append(
                    f"{rel}: uses shared symbol `{name}` at {where} but never imports it "
                    f"(not in bound imports / own exports / local declarations)"
                )
    assert not problems, (
        "shared symbols referenced without an import (would be a runtime ReferenceError "
        "in ESM strict mode):\n  " + "\n  ".join(problems)
    )
