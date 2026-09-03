# Overhaul 实施日志

> 依 `redesign-audit/05-overhaul-exec-spec.md` 逐 Lever 追加。每个 Lever 一颗 commit。

## Lever 1 — dual theme system (light default + dark)
- 状态: ✔ done
- 改动文件: web/index.html, web/css/app.css, web/js/app.js, web/js/icons.js
- commit: (填充) feat(web): dual theme system (light default) with flash-free bootstrap + dark skin
- 说明: 拆分 `:root{}` 为 type/spacing/radius/motion 全局 token（保留不变）+ `:root[data-theme="light"]` 浅色色板，新增 `:root[data-theme="dark"]` 暖黑深色色板并逐 pair 补齐。index.html 注入防闪烁 bootstrap（强制设 `data-theme`，LS 无值默认 light，避免选择器不匹配导致无字色）。topbar 新增 `.iconbtn` 切换钮，`app.js` 加 `theme/toggleTheme`（写 `cb_gw_theme`），`icons.js` 新增 `sun/moon`（stroke 1.8）。
- screenshot: 未截屏