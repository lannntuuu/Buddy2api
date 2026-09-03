# Stage 3 Mode + Strategy

## Mode（覆盖确认）

**Overhaul**。视觉与布局语言重做，信息架构 / 路由 / 功能 / 文案 / API 契约全保留。
判定依据：CSS 两代主题叠写已到"补丁摞补丁"状态（审计 #3、#6），Preserve 的增量修补成本高于重写样式层；且用户明确选择 Overhaul。

## Three Dials

```
Current dial reading（基线）
DESIGN_VARIANCE:   4   （橙 accent + 深顶栏，整体仍是通用后台观感）
MOTION_INTENSITY:  1   （几乎无过渡，一处 translateY hover）
VISUAL_DENSITY:    7   （信息密度高，表格/热力图/多指标卡密集）

Target dial reading（Overhaul 后）
DESIGN_VARIANCE:   6   （统一亮色 + 橙 accent 强化层次，图表成为视觉主角）
MOTION_INTENSITY:  3   （150ms 基础过渡 + toast/modal 进出场 + 图表微动效，均低于 prefers-reduced-motion 阈值）
VISUAL_DENSITY:    7   （密度保持——这是运维工具，密度是功能）
```

## Design Read

> *Reading this as: 本机自用的多通道 API 网关运维台 for 开发者，with a 克制工程风（calm-engineered）语言，leaning toward 无构建 Vue 3 + 原生 CSS 令牌 + 暖白底 + 单一橙 accent + mono 数字强调。*

参照系：Linear / Vercel dashboard 的信息密度 + Ville 例（本机工具）的克制装饰。不做营销页式 hero，不做 bento，不做渐变装饰。

## Overhaul 边界（红线）

1. 9 个页面 key、导航结构与顺序：不变
2. 所有 /admin/* 与 /v1/* 端点契约：不变
3. localStorage 键（cb_gw_page、cb_gw_token）：不变
4. 全部业务文案：不变
5. 拖拽排序（SortableJS）交互行为：不变
6. 无构建步骤原则保留（原生 ESM + CSS，不引入打包器）

## 实施顺序（两个并行流）

### 流 A：Web Overhaul（按 lever 推进）

| Lever | 内容 | 对应审计项 |
|---|---|---|
| W1 | 依赖本地化：Vue/Sortable → ops/vendor/，index.html 改本地引用，断网验证 | E1 |
| W2 | 版本号单一来源：gateway/version.py → /admin/meta 或启动注入 → app.js 读取 | E2, #12 |
| W3 | CSS 重建：删除死层，单一令牌体系（type scale / 半径 / 色板 / 间距），顶栏并入亮色 | #1-#4, #6, #14, B1/B2 |
| W4 | 组件层重做：btn/badge/card/table/modal/toast/表单 控件对齐新令牌 + 150ms 过渡 + focus-visible | #9-#11, #15 |
| W5 | 图表令牌化：热力/cache/三通道色板迁入 :root，JS 内联色清零 | #7, #8, B3 |
| W6 | Dashboard/账号页重点页视觉重排（信息密度不变，层次重做） | #2, #18 |
| W7 | 移动端断点收敛 1180/760 + 全页面回归截图 | #13, #18 |
| W8 | 清理根目录一次性脚本 → ops/scripts/oneoff/ | E3 |

### 流 B：后端拆分（与流 A 无文件交集，可并行）

| Lever | 内容 | 验收 |
|---|---|---|
| P1 | storage/database.py → 按域拆 repositories（accounts/keys/logs/stats/channels）+ 兼容门面，import 路径不变 | pytest 全绿 |
| P2 | gateway/server.py → routers/（v1、admin、static、meta）+ app 工厂 + lifespan 收口 | pytest 全绿 + 56 端点数量不变 |
| P3 | upstream/proxy.py → pipeline：sse 规范化 / 内容审核 / 重试 / 转发分层 | pytest 全绿 |
| P4 | 根目录清理（与 W8 同一改动，归属流 B 执行） | git status 干净 |

## 回归策略

- 流 A 每 lever 后：9 页面截图对比 + 手动过一遍导航/CRUD/拖拽
- 流 B 每 lever 后：`pytest` 全量（6029 行）+ 端点计数断言
- 红线复查在 Stage 7 统一执行（路由/导航/表单/文案/契约五项）
