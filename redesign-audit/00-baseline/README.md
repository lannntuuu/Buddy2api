# Stage 1 Baseline（重构前基线）

日期：2026-07-19 · 基线 commit：`dc01539`（refactor(web): split monolithic index.html into modular assets）

## 页面清单（IA 保留，Overhaul 只换视觉）

| # | 页面 key | 导航名 | 职责 | 主要 API | js 行数 |
|---|---|---|---|---|---|
| 1 | dashboard | 运行总览 | 健康状态 + 今日用量 + 额度 | /admin/stats, /admin/credit-summary, /admin/credit-overview | 148 |
| 2 | accounts | 账号管理 | 5 通道账号检测/导入/测试 | /admin/accounts/* | 188 |
| 3 | quota | 额度与积分 | 各通道余额明细 | /admin/credit-* | 78 |
| 4 | keys | API Keys | Key 创建/绑定通道 | /admin/keys | 55 |
| 5 | channels | 通道与模型 | 通道开关/排序（拖拽） | /admin/channels | 219 |
| 6 | usage | 用量统计 | 历史聚合 | /admin/stats | 66 |
| 7 | logs | 请求日志 | 日志查询 | /admin/logs | 34 |
| 8 | setup | 接入指南 | 客户端配置指引 | 静态 | 134 |
| 9 | settings | 设置 | Admin Token 等 | /admin/settings | 175 |

导航结构、页面职责、URL 路由（localStorage 键 `cb_gw_page`）、全部接口契约：**红线，不变**。

## 技术栈基线

| 层 | 现状 |
|---|---|
| 框架 | Vue 3.4.21 global build，jsdelivr CDN（断网白屏） |
| 拖拽 | SortableJS 1.15.6，jsdelivr CDN |
| 构建 | 无。ESM `<script type="module">`，浏览器原生加载 |
| 样式 | 单文件 `web/css/app.css` 385 行，两代主题叠写 |
| 图表 | 手写 div（热力格、24h 柱状、sparkline），色值硬编码在 JS |
| 后端 | FastAPI，`python -m gateway.server`，56 端点，无前端构建依赖 |

## 核心模块体量（重构对象）

| 模块 | 行数 | defs | 内容 |
|---|---|---|---|
| gateway/server.py | 1856 | 85 | 56 端点 + 启动逻辑 + 控制面混杂 |
| upstream/proxy.py | 1688 | 35 | SSE 规范化 / 审核 / 重试 / 代理耦合 |
| storage/database.py | 1525 | 55 | 全部表操作单文件 |
| accounts/control_plane.py | 894 | 35 | |
| accounts/auth_manager.py | 1041 | 47 | |

## 基线问题清单（Stage 2 审计的输入）

### A. 工程 / 功能风险
1. **A1（critical）** Vue + Sortable 走 jsdelivr CDN：断网 / CDN 被墙 → 管理页白屏。本机工具的可用性硬伤。
2. **A2（important）** `app.js` 品牌区写死 `v2.1.0`，实际 2.2.0；历史上已出过一次同类 bug（`codex/fix-ui-version-label`）。
3. **A3（important）** 根目录散落一次性脚本（`_analysis_*.py` ×4、`_backfill_*.py` ×2，约 20KB）与 `.tmp/` 调试残留，污染仓库根。
4. **A4（suggestion）** CSS 中残留未使用变量（--blue 系已别名到 accent，但仍存在双套名字）。

### B. CSS 架构
5. **B1（critical）** 死代码层：app.css 第 1-300 行是旧版侧边栏布局（`.side` / `.side-nav` / `.side-foot` / `.nav-grp`），JS 零引用；第 301-385 行才是现行顶栏方案的覆盖层。每条规则都要被后写的层"打补丁"，维护成本高。
6. **B2（critical）** 半径无体系：4px（badge/tag）、5px（today-period）、6px（btn/nav/codeblk）、7px（topbar 元素）、8px（card/modal）混用。
7. **B3（important）** 图表色板硬编码：dashboard.js `heatStyle`（6 档 hex）、`cacheStatusColor`（4 hex）、`sparkHeight`/`hourBarHeight` 等内联样式。
8. **B4（important）** 亮暗割裂：顶栏 `#1d1c1a` 深色 + 主体浅色，无暗色模式（用户确认仅亮色 → 顶栏并入亮色体系）。

### C. 交互 / 视觉
9. **C1（important）** 图表无加载/空态之外的动效层次；hover 只有 `translateY(-1px)` 一处。
10. **C2（suggestion）** 字体栈无显式 fallback 顺序问题；--mono 仅用于数字，展示层无层次（18px h1 vs 21px 覆盖层 h1 两套）。
11. **C3（suggestion）** 表格 hover `#fff9f3`、热力色阶（#fff0ce→#f15f14）等一次性色值未进令牌。

## 测试安全网

`tests/` 22 个文件，约 6029 行，含 `test_dashboard_perf.py`、`test_control_plane.py`、5 通道各自测试。后端拆分的每步必须保持全绿。

## Lighthouse / 截图

本机工具无公开 URL，Lighthouse 基线以 Stage 6 实施时的对比测量替代；截图对比在实施 lever 时逐页留存。
