# Stage 2 Six-Dimension Visual Audit

范围：web 管理页（9 页面共享 app.css + 各自模板）。行号基于 `web/css/app.css` @ dc01539。

| # | 维度 | 位置 | 严重度 | 现状 | 修复方向 |
|---|---|---|---|---|---|
| 1 | Layout | app.css:301-313 | critical | 顶栏深色 `#1d1c1a` + 主体亮色，同一页两套明度体系 | Overhaul：统一亮色体系，顶栏白底 + 细分割线，深色只留给 codeblk |
| 2 | Layout | app.css:301 | important | 顶栏 72px 高，nav 居中挤压 brand 与 actions | 顶栏降至 56px 双层信息（brand 左 / nav 左对齐 / actions 右） |
| 3 | Layout | app.css:20-22 | critical | `.side`/`.main{margin-left:220px}` 旧侧边栏布局与顶栏布局叠写 | 删除死层（1-300 行重写），单一布局：sticky 顶栏 + 内容区 max-w 1600 |
| 4 | Typography | app.css:14, 318 | critical | `font-size:14px` 基准被覆盖层 `th 10px`、`.hour-y-axis 9px` 等多档硬编码；h1 两套（18px/21px） | 建立 type scale：11/12/13/15/18/22/28，令牌化 --fs-* |
| 5 | Typography | app.css:11 | important | --mono 只用于数字；代码块、模型名、Key 前缀混用 | 数字与代码统一 mono 令牌；模型名/Key 用 mono + 微弱背景 |
| 6 | Color | app.css:2-13 | critical | 双套语义色（--blue 系 + --accent 系别名）并存；一次性 hex 40+ 处 | 单一 accent（橙 #e85d13 保留为品牌色）+ 语义令牌全套；--blue 系删除 |
| 7 | Color | dashboard.js:39 | important | 热力图 6 档色、cache 状态 4 色硬编码在 JS | 迁入 :root 图表色板令牌（--heat-1..6、--cache-*） |
| 8 | Color | app.css:335 | important | 三张图表各有一套 --chart 覆写（橙/青/蓝灰） | 保留三通道区分但全部令牌化，明度关系统一 |
| 9 | Interactivity | 全局 | important | 过渡只有 .1s linear 或无；无 focus-visible 体系（仅 ch-card 一处） | 统一 150ms ease-out；所有可交互元素 focus-visible 2px accent outline |
| 10 | Interactivity | app.css:33-34, 74 | suggestion | hover 全靠背景变色，无 elevation 反馈 | 行 hover + 表格 hover 用同一 hover 令牌；按钮 hover 加 1px 位移阴影 |
| 11 | Interactivity | app.css:256-262 | suggestion | toast 无进出场动画 | slide-in 200ms + auto dismiss 保持 2.5s |
| 12 | Content | app.js:29 | critical | 品牌区写死 "v2.1.0"，实际 2.2.0 | 从 gateway/version.py 注入 /admin/meta 或模板变量，单一来源 |
| 13 | Content | app.css:308, 341 | suggestion | 图表轴标签 9px mono 过小，可读性差 | 提到 10px 并保持 mono |
| 14 | Component | app.css:78-80 | important | badge 圆点伪元素 + 4px 半径，与 btn 6px、card 8px 不成体系 | 单一半径体系：--r-s 4 / --r-m 6 / --r-l 8，映射到组件 |
| 15 | Component | app.css:193-201 | suggestion | modal 无进出场、遮罩无 blur 层级说明 | 进出场 150ms + 遮罩 rgba(29,28,26,.5) 统一 |
| 16 | Component | 各页模板 | important | 空态只有 `.empty` 一种，加载只有 spin | 保留现状骨架（Overhaul 不动逻辑），样式令牌化 |
| 17 | Iconography | icons.js | suggestion | 自绘 SVG 15 个，风格统一（可保留） | 保留，仅统一 stroke-width 1.5 |
| 18 | Layout | app.css:376-380 | important | 移动端断点 1180/900/760 三套规则分散 | 收敛为 1180/760 两档，规则就近放置 |

严重度统计：critical 6 · important 8 · suggestion 4。全部条目有修复方向，无 tbd。

## 工程类发现（随 Stage 3 一起修）

| # | 位置 | 严重度 | 现状 | 修复方向 |
|---|---|---|---|---|
| E1 | index.html:11-12 | critical | Vue/Sortable 走 jsdelivr CDN，断网白屏 | 下载至 ops/vendor/，网关 /static 直接服务 |
| E2 | app.js:29 | critical | 版本写死 | version.py 单一来源注入 |
| E3 | 根目录 | important | _analysis_*.py ×4、_backfill_*.py ×2、.tmp/ 残留 | 移入 ops/scripts/oneoff/ 或删除（git 历史已保留） |
