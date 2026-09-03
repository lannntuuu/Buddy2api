# Redesign Decisions

## 1. Mode
- [ ] Preserve（visual upgrade, IA + content + URLs unchanged）
- [x] Overhaul（visual new, IA + content preserved）
- [ ] Greenfield（full rebuild）

用户确认：Web 管理页走 Overhaul；信息架构（9 个页面、导航结构、URL）、功能、文案全部保留，视觉与布局语言重做。
同时确认：后端代码重构（server.py / proxy.py / database.py 拆分）与 Web 重构并行，两条流解耦。

## 2. Business KPI
本项目是本机自用开发者工具，无 GA / 转化指标。替代 KPI：

| KPI | 基线 | 目标 |
|---|---|---|
| 管理页可用性（断网可打开） | 否（Vue 走 jsdelivr CDN，断网白屏） | 是（vendor 本地化后 0 外部请求） |
| CSS 死代码 | 约 300 行旧版侧边栏样式（.side 系，JS 零引用） | 0 |
| 设计令牌 | 半径 4/5/6/7/8px 混用；图表色硬编码在 JS | 单一半径体系 + 全部色值收进 :root 变量 |
| 版本号单一来源 | app.js 写死 v2.1.0（实际 2.2.0） | 从 gateway/version.py 注入 |
| pytest | 6029 行全绿 | 每步重构后保持全绿 |

## 3. SEO Risk Level
- [x] Low（本机工具，127.0.0.1，无搜索引擎暴露。SEO 检查项整体跳过，仅保留路由不变约束）

## 4. Collaboration
- [x] Solo（用户拍板方向，agent 实施，每阶段交付物落盘供审阅）

## 附加决策
| 项 | 决定 |
|---|---|
| 暗色模式 | 不做。仅亮色主题（用户确认）。现行顶栏深色块在 Overhaul 中一并处理成统一亮色体系 |
| Vue / Sortable CDN | 本地化到 ops/vendor/，由网关 /static 直接服务，断网可用 |
| 后端范围 | server.py（1856 行）、proxy.py（1688 行）、database.py（1525 行）拆分 + 根目录一次性脚本清理 |
