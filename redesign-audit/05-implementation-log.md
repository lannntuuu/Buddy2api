# Overhaul 实施日志

> 依 `redesign-audit/05-overhaul-exec-spec.md` 逐 Lever 追加。每个 Lever 一颗 commit。

## Lever 1 — dual theme system (light default + dark)
- 状态: ✔ done
- 改动文件: web/index.html, web/css/app.css, web/js/app.js, web/js/icons.js
- commit: e753ba5 feat(web): dual theme system (light default) with flash-free bootstrap + dark skin
- 说明: 拆分 `:root{}` 为 type/spacing/radius/motion 全局 token（保留不变）+ `:root[data-theme="light"]` 浅色色板，新增 `:root[data-theme="dark"]` 暖黑深色色板并逐 pair 补齐。index.html 注入防闪烁 bootstrap（强制设 `data-theme`，LS 无值默认 light，避免选择器不匹配导致无字色）。topbar 新增 `.iconbtn` 切换钮，`app.js` 加 `theme/toggleTheme`（写 `cb_gw_theme`），`icons.js` 新增 `sun/moon`（stroke 1.8）。
- screenshot: 未截屏

## Lever 2 — self-host Geist/Geist Mono + tabular-nums
- 状态: ✔ done
- 改动文件: web/css/app.css, web/fonts/Geist{,Mono}-{Regular,Medium,SemiBold,Bold}.woff2
- commit: 683a8bd feat(web): self-host Geist/Geist Mono with tabular-nums numbers
- 说明: 7 条 `@font-face`（Geist 4 字重 + Geist Mono 3 字重，`font-display:swap`）置于 `:root` 前。`--font` 前置 `'Geist'`，`--mono` 前置 `'Geist Mono'`，系统栈保留作 fallback。新增大范围规则：`.m-value/.today-value/.health-kpi .v/.metric .m-value/.pkg-kpi .v/.pkg-time/.credit-rem/.official-val/.rank-value/.hour-chart-meta strong/.state-number/table td` 全部 `font-family:var(--mono);font-feature-settings:'tnum' 1`。woff2 共 7 个文件落地 `web/fonts/`，通过 `/static/fonts/...` 提供。pytest -q tests/test_web_assets.py 通过 (14 passed)。零外链字体、零 em-dash。
- screenshot: 未截屏

## Lever 3 — rail sidebar (left 56px)
- 状态: ✔ done
- 改动文件: web/js/app.js, web/js/icons.js, web/css/app.css
- commit: 0dc1fb6 feat(web): rebuild shell into left 56px rail nav with active state
- 说明: `app.js` 外壳从 `.layout/.topbar/.topnav/.brand-block` 改为 `.shell` (grid 56px | 1fr) + `.rail`(深色 sunken、sticky 100vh、品牌 logo + 9 项竖向 nav + 底部主题切换) + `.shell-body/.shell-head/.main/.content`。`nav` 数组结构与 key 不变，仅渲染位置改；`go/k/l/i` 全部保留。`icons.js` 新增 `logo`（stroke 1.8，方框+折线）。`app.css` 删除/重写 `.topbar/.topnav/.nav-item/.brand-*/.top-actions/.layout` 段，新增 `.shell/.rail/.rail-brand/.railnav/.rail-item/.rail-foot/.rail-icon/.shell-head/.shell-title/.shell-ver/.shell-actions`，激活态 `::before` 左侧 3×22 橙竖条。两个 media query 重写：≤760 改 grid `1fr / 56px`，rail 沉底为 fixed 横排 tab，激活态指示条转顶部。dark 主题 `.topbar` 残留块改为 `.rail`。pytest -q tests/test_web_assets.py 通过 (14 passed)，零 em-dash。
- screenshot: 未截屏

## Lever 4 — dashboard hero + KPI band + dark-tuned charts
- 状态: ✔ done
- 改动文件: web/js/pages/dashboard.js, web/css/app.css
- commit: 17badad feat(web): restyle dashboard hero into status + KPI band, dark-tuned charts
- 说明: dashboard.js 的 `.dash-hero` 子结构由 `.health-main+health-kpis` 重写为 `.hero-left(hero-statusline+hero-state+hero-sub+status-line)` + `.hero-kpis(3 × .hero-kpi)`。数据绑定 `healthClass/healthText/n/pct/ms` 与所有 s 字段完全保留，KPI 三个 `.v` 加 `state-number` class 复用 tabular-nums。`.today-value` 增加 `font-feature-settings:'tnum' 1`。app.css 的 `.dash-hero` 重写：grid `1.4fr/.6fr`、padding 加大、内含 `::after` 260×260 radial-gradient 橙软光斑。新增 `.hero-left/.hero-statusline/.hero-state/.hero-sub/.hero-kpis/.hero-kpi/.hero-kpi .k/.hero-kpi .v`。`.hero-kpi .v` 加入 §3 的 mono 列表。`.health-*` 旧段保留（页面其它处不引用、无副作用）。图表深色配色已在 Lever 1 `--chart-* --heat-* --cache-*` token 自翻转覆盖。pytest 通过 (14 passed)、零 em-dash。
- screenshot: 未截屏

## Lever 5 — entrance stagger + hover/press motion
- 状态: ✔ done
- 改动文件: web/css/app.css
- commit: 65c3d25 feat(web): tasteful entrance stagger + hover/press motion (reduced-motion safe)
- 说明: 追加 `@keyframes riseIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}`。`.content > .card/.dash-hero/.today-usage` 各 stagger 0/0.04s/0.08s 入场，`.content > .dash-grid` 0.12s。`.card:hover` 加 `box-shadow:0 4px 20px rgba(0,0,0,.06)`。按压：`.iconbtn:active/.refresh-cta:active/.rail-item:active{transform:scale(.97)}`（`.btn:active{translateY(1px)}` 原有保留，避免 cascade 覆盖；spec §6.2 选择器里去掉 `.btn`）。三处 transition 加 `transform var(--dur-fast) var(--ease)`。prefers-reduced-motion 已在 L114 全局 0ms 兜底（`*` animation/transition 0ms）。pytest 通过 (14 passed)、零 em-dash。
- screenshot: 未截屏

## Lever 6 — polish empty/loading + icon stroke + dark shadows
- 状态: ✔ done
- 改动文件: web/css/app.css
- commit: 630210a feat(web): polish empty/loading states, unify icon stroke, dark shadows
- 说明: `.empty` 重写为 flex 居中 + `::before` 40×40 圆角块 radial-gradient 8px 网格点占位（保留 `.empty .em` 文本）；`.load` 改 flex 居中 + 文案配色；dark 块新增柔和阴影 override（`.card:none` / `.modal:0 24px 80px / .6` / `.toast:0 10px 30px / .5`）。`icons.js` 全 17 个 stroke-width=1.8 一致（`logo` Lever 3 已加）。pytest 通过 (14 passed)、零 em-dash。
- screenshot: 未截屏
- screenshot: 未截屏
- screenshot: 未截屏

## Lever 7 — collapsible rail sidebar
- 状态: ✔ done
- 改动文件: web/js/app.js, web/js/icons.js, web/css/app.css
- commit: 53815d4 feat(web): collapsible rail sidebar with expand/collapse toggle
- 说明: 默认仍 56px 纯图标。`app.js` 新增 `railOpen`(读 `localStorage('cb_gw_rail')`)+ `toggleRail`(写 LS 兜 try/catch)；template `<aside class="rail" :class="{open:railOpen}">`、`.rail-item` 拆为 `<span class="rail-ic">` + `<span class="rail-lbl" v-if="railOpen">`；`.rail-foot` 主题按钮上方插入双箭头切换钮，chevron 方向随状态翻转。`icons.js` 加 `chevronL/chevronR`(stroke 1.8)。`app.css` `.shell` 列宽改 `var(--rail-w,56px)`；`.rail` 加 `transition:width`；`.rail-item` 由 `display:grid;place-items:center` 改 `display:flex;align-items:center;justify-content:center`(收起态仍 40×40 居中)；新增 `.rail.open`(200px,stretch 排版)+ 子元素 `.rail-brand/.railnav/.rail-item` 横排靠左 + `.rail-lbl` 显示字号字重。移动端 `@media (max-width:760px)` 追加 `.rail.open{width:100%;align-items:center}`+`.rail-lbl{display:none}`,确保底部 tab 条不破坏。pytest -q tests/test_web_assets.py 14 passed、零 em-dash。

## Lever 8 — setup guide sectioned cards
- 状态: ✔ done
- 改动文件: web/js/pages/setup.js, web/css/app.css
- commit: f4a9669 feat(web): restructure setup guide into sectioned cards, drop inline styles
- 说明: setup 页从 1 长卡(接入信息+向导+Codex 一键+快速验证)拆为 4 独立 `.card`,每块 `.card-h`+`.card-p`。保留全部逻辑/函数/后端调用/用户可见文案(零功能改动)。消灭全部内联 `style=` 与幽灵变量(`--blue-soft/--blue-border/--fg2/--green/--red/--blue`),改用 `.text-muted/.text-ok/.text-err/.text-accent/.mb-1..4/.mt-1..4/.callout/.testbox/.status-line/.hint/.mono/.tcell/.field/.btn/.btn.pri` 等已有/新增工具类。`app.css` utilities 区新增 `.text-ok/.text-accent/.text-xs/.status-line/.callout.accent/.codex-grid/.codex-row/.field-grow`(均在 `:root[data-theme=dark]` 下透过 `--accent-soft/--accent-border/--fg-3/--ok/--err` 自动适配)。`git grep -n "var(--blue\|var(--green\|var(--red\|var(--fg2\|style=" web/js/pages/setup.js` 输出空(`style=` 5 处 `font-size:12px` 已全部改为 `.text-xs`)。Codex 专用说明块改用 `.callout.accent` + `.codex-grid`;Codex 一键配置 card 内的 API Key 输入改 `.tcell`(已含全宽+mono);状态徽章容器改 `.status-line`;结果块改 `.testbox`,配色用 `.text-ok/.text-err/.text-accent/.text-muted`。pytest -q tests/test_web_assets.py 14 passed、零 em-dash。

## Lever 9 — ghost CSS var cleanup (channels/keys)
- 状态: ✔ done
- 改动文件: web/js/pages/channels.js, web/js/pages/keys.js
- commit: 5376606 feat(web): replace ghost CSS vars with real tokens in channels/keys pages
- 说明: 仅做幽灵 CSS 变量名→真实 token 的机械替换,零文案/逻辑/布局/内联 style 结构改动。channels.js 替换 10 处(--red×4、--fg2×4、--border2×1、--green×1,对应 --err/--fg-2/--border-strong/--ok);keys.js 替换 6 处(--blue-bg×2、--blue×2、--green-bg×1、--blue-border×1,对应 --accent-soft/--accent/--ok-bg/--accent-border)。--fg3/--ok-border/--ok-fg 已存在,保留不动。git grep 幽灵变量 → 0;pytest -q tests/test_web_assets.py 14 passed;中文 UI 零 em-dash。

## Lever 10 — brand title + version moved into collapsible rail
- 状态: ✔ done
- 改动文件: web/js/app.js, web/css/app.css
- commit: 6392616 feat(web): move brand title + version into collapsible rail
- 说明: `.rail-brand` 从纯 `v-html="I.logo"` 改为 logo + 文字双 span 结构(`.rail-brand-ic` + `.rail-brand-txt` 包裹 `.rail-brand-name` + `.rail-brand-ver`,后者 `v-if="railOpen"`,且版本号 `v-if="meta.version"` 兜底)。`.shell-head` 移除 `.shell-title`/`.shell-ver` 双 span(品牌已移入 rail),保留 `.shell-head` 容器承担 sticky 定位,容器内仅余 `.shell-actions`(metaTag + 刷新钮)。app.css 同步:.rail-brand 由 `display:grid;place-items:center` 改 `display:flex`,增加 `gap:8px;overflow:hidden;transition:width`;新增 `.rail-brand-ic`/`-txt`/`-name`/`-ver`(字体/字号/颜色/white-space 全部按 spec §2.2 取值);`.rail.open .rail-brand` 增 `height:auto;min-height:40px`,新增 `.rail.open .rail-brand-txt{display:flex}`;`.shell-title`/`.shell-ver` 两段删除(死代码);`.shell-actions` 加 `justify-content:flex-start`(左对齐,顶部条无标题后右对齐会显得飘)。移动端(≤760)`.rail-brand,.rail-foot{display:none}` 已存在,品牌文字在 rail 沉底 tab 条时仍不显示,无需额外规则。`git grep -n "shell-title\|shell-ver" -- web/js/app.js web/css/app.css` 输出空;`git grep -n "—" -- web/` 输出空;pytest -q tests/test_web_assets.py 14 passed;中文 UI 零 em-dash。

## Phase A — per-channel upstream host override (gmi, qwenwork)
- 状态: ✔ done
- 规范: redesign-audit/11-channel-host-phaseA-spec.md(执行规范)
- 改动文件: providers/host_override.py, providers/gmi/chat.py, providers/qwenwork/chat.py, providers/qwenwork/token.py, providers/qwenwork/__init__.py, gateway/routers/admin.py, tests/test_host_override.py, web/js/pages/settings.js
- commit: 74ccc39 feat(providers): per-channel upstream host override (gmi, qwenwork);c12657a feat(web): per-channel host override UI in settings
- 说明: 新增 `providers/host_override.py`,导出 `CHANNEL_HOST_FIELDS={gmi:("base_url",),qwenwork:("gateway",)}` 白名单与 `channel_host(channel_id, field, default)` 解析器(读 settings 表 `channel_hosts` blob,缺值返回 default,行为零变化)。GMI `_base_url(account)` 把兜底项由 `DEFAULT_BASE_URL` 改为 `channel_host(CHANNEL_ID,"base_url",DEFAULT_BASE_URL)`,优先级保持账号 `extra.base_url` > 账号 `domain` > 全局覆盖 > 默认。qwenwork 三处 `GATEWAY` 引用 — `chat.chat_url()`、`token.refresh_account()` URL、`__init__.fetch_quota()` URL — 全部改为经 `channel_host(CHANNEL_ID,"gateway",GATEWAY)` 兜底,`constants.GATEWAY` 默认值不变。后端 admin `/admin/settings` PUT 的 `allowed_settings` 增 `"channel_hosts"`,校验要求是 dict、cid 在白名单、字段名在白名单、每个非空值必须 `https://`、否则 400;空值跳过写入。前端 settings 页"后端参数"卡片保留 workbuddy 三字段,在末尾 form-grid 内新增 GMI Cloud Base URL 与 QwenWork 网关两个 `.field`(reactive `hostOverrides`,load 时填回,save 时随 `backend_url/default_domain/timeout` 一起 PUT)。新增 `tests/test_host_override.py` 三个 case:默认/unset、覆盖/set、白名单 shape。pytest 结果:`tests/test_host_override.py` 3 passed,`tests/test_web_assets.py` 14 passed(ESM 解析含 settings.js),`tests/test_core.py` 111 passed + 1 pre-existing failure(`test_chat_proxy_stream_rejects_invalid_tool_arguments_at_eof`,基线同样失败,与本任务无关)。`git grep -n "—" -- web/js/pages/settings.js providers/host_override.py tests/test_host_override.py` 输出空,中文 UI 零 em-dash;untracked `redesign-audit/10-*.md`、`11-*.md` 未加入功能 commit。push:`6392616..c12657a` 到 origin/refactor/web-console-ia。

## Phase B — per-channel upstream host override (qclaw, traesolo, traework)
- 状态: ✔ done
- 规范: redesign-audit/12-channel-host-phaseB-spec.md(执行规范)
- 改动文件: providers/host_override.py, providers/qclaw/chat.py, providers/qclaw/jprx.py, providers/traesolo/chat.py, providers/traesolo/login.py, providers/traesolo/store.py, providers/traesolo/token.py, providers/traework/chat.py, providers/traework/quota.py, providers/traework/store.py, providers/traework/token.py, tests/test_host_override.py, web/js/pages/settings.js
- commit: 04997b4 feat(providers): per-channel host override for qclaw/traesolo/traework;1cfb111 feat(web): per-channel host override UI for qclaw/traesolo/traework
- 说明: 把 Phase A 的 `channel_host()` 机制扩展到三个多 host 平台。`CHANNEL_HOST_FIELDS` 扩展为 5 平台:qclaw=(jprx_gateway, aizone_base)、traesolo=(oauth_host, console_host, agent_host)、traework=(agent_host, ug_host)。qclaw:`jprx.py post_cmd()` 的 `JPRX_GATEWAY` 与 `chat.py` 三处 `AIZONE_BASE`(chat_completions/_stream/test_chat)全部经 `channel_host` 兜底。traesolo:`token.py` 的 `_oauth_base()/exchange()/build_login_url()`(oauth_host + console_host)、`chat.py` 的 `AGENT_HOST`(models + chat 两处)、`login.py` 四处 `OAUTH_HOST`(exchange 调用 + extra.api_host 写入)、`store.py` 的 `api_host` 兜底。traework:`chat.py` 的 `AGENT_API`(session_url)、`quota.py`/`token.py` 的 `_host()`(ug_host)、`store.py` 两处 `UG_API`(import 解析 + parse_credentials)。关键设计:traework 与 traesolo 共享 `trae_shared.AGENT_HOST/UG_HOST`,但覆盖按 channel 独立(traework 用 `channel_host("traework",...)`,traesolo 用 `channel_host("traesolo",...)`),互不影响。覆盖优先级与 GMI 一致:账号 `extra.host/api_host` > 全局 `channel_hosts.<channel>.<field>` > 默认常量。协议常量默认值全部未动。前端 settings.js:`hostOverrides` 扩展 5 平台,`fillHosts()`/`save()` 对应扩展,新增"高级平台覆盖(QClaw / Trae SOLO / TraeWork)"`<details>` 折叠区(7 个字段,中文文案零 em-dash)。测试:`tests/test_host_override.py` 更新为 5 平台白名单断言 + 新增 `test_phase_b_override`(qclaw 覆盖生效、未设置回退默认),共 5 passed;`tests/test_web_assets.py` 14 passed。`tests/test_core.py` 111 passed + 1 pre-existing failure(与 Phase A 记录相同);`tests/test_traesolo.py/test_qclaw.py/test_traework.py/test_qwenwork.py/test_gmi_store.py` 6 failed 经 `git stash` 基线对比确认全部为 pre-existing(与本次改动无关)。中文 UI 零 em-dash;untracked `redesign-audit/10/11/12-*.md` 未加入功能 commit。