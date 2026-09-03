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
- commit: (pending) feat(web): polish empty/loading states, unify icon stroke, dark shadows
- 说明: `.empty` 重写为 flex 居中 + `::before` 40×40 圆角块 radial-gradient 8px 网格点占位（保留 `.empty .em` 文本）；`.load` 改 flex 居中 + 文案配色；dark 块新增柔和阴影 override（`.card:none` / `.modal:0 24px 80px / .6` / `.toast:0 10px 30px / .5`）。`icons.js` 全 17 个 stroke-width=1.8 一致（`logo` Lever 3 已加）。pytest 通过 (14 passed)、零 em-dash。
- screenshot: 未截屏
- screenshot: 未截屏
- screenshot: 未截屏