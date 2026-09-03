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