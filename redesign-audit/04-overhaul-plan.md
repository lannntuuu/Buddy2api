# Overhaul 视觉重做方案 (Mode C · 推倒重来)

> 目标：这次让用户**肉眼一秒看出是"新设计"**，不是 token 微调。
> 范围：仅前端视觉层。IA / 路由 / URL / 导航标签 / 表单字段名 / 后端接口**一律不动**。

---

## 0. 诊断：为什么上一轮 "看不出变化"

上一轮做的是「色彩 / 排版 / 导航三件套」的 token 化重写。用户感知不到变化，根因有三：

1. **字体没换**（沿用 system 栈）→ 整体观感权重最大的一项没动
2. **页面排版层级没动**（dashboard 仍是"左大右小"网格、card-h + card-p 结构、顶栏一排 icon 导航）→ 还是那个"老朋友"
3. **没有 dark mode、没有进场动效、图表配色没拉开、icon 没统一** → 这些是"肉眼秒判新设计"的强信号

**这轮要打的是结构 / 风格 / 品牌定位**，不是 token 微调。

---

## 1. 设计读 Read (一次讲清方向)

> **Reading this as:** 开发者本地运维的控制台（admin console for local API gateway users），服务对象是**技术向用户**（跑 AI 网关的开发者），用 **"冷静工程感 / 深色终端气质"** 的语言，倾向 **monospace-terminal + 深色底 + 强橙色高亮** 的工程美学。

不是营销落地页，不套 Dribbble/Awwwards 那套花哨。是 **Linear / Vercel / Warp terminal** 那一族的工程终端风。

## 2. Three Dials (当前 vs 目标)

| 拨盘 | 当前值 | 目标值 | 说明 |
|------|--------|--------|------|
| DESIGN_VARIANCE | 4 | 6 | 从"规则三列"走向非对称分区 |
| MOTION_INTENSITY | 2 | 5 | 从"基本无动效"走向克制但明显的动效层 |
| VISUAL_DENSITY | 4 | 4 | 数据控制台本来就密，保持 |

Overhaul 规则：VARIANCE +2、MOTION +?（目标显著提升）、DENSITY 持平。

---

## 3. 具体改造轴 (每轴一个 lever，按序执行)

### Lever 1 · 明暗主题 + 深色外衣（最大视觉冲击）
- **新增 dark + light 双主题**（`prefers-color-scheme` + 手动切换，默认跟随系统）
- 深色是**主打皮肤**：暖黑底 + 终端式绿/橙五行色，符合"AI 网关终端"定位
- 重新校准 token：`--bg`、`--bg-elevated`、`--fg`、`--border`、`--accent` 全部拆成 `--light-*` / `--dark-*`，用 CSS 变量在 `[data-theme]` 上翻转
- 语义色（ok/warn/err）在深色下要重新校对比度，**绝不白字白底**

### Lever 2 · 换字体（品牌识别核心）
- 引用 `--font` 从 system 栈 → **Geist / Inter Display 类工程 sans + Geist Mono / JetBrains Mono 等宽**
- 自托管：从 `web/` 下建 `fonts/`，用 `@font-face` + `font-display:swap`（**绝不外链 Google Fonts**）
- display/数字/统计全部走 mono（`tabular-nums`），强化"终端数据感"

### Lever 3 · 导航布局重建（结构可见变化）
- 顶栏整排 icon 导航 → **左侧竖向 "rail" 侧边栏**（56px 图标轨 + 深色底），替代"青色一排 chip"的老观感
- 布局从 `flex-column` 改为 **CSS Grid 两分区**：`grid-template-columns: 56px 1fr`
- 激活态不再是"浅橙底"而是**左侧竖条 + 图标变色**（终端命令 `>` 提示符式的激活标记）
- 移动端 rail 收起为底部 tab 条或汉堡

### Lever 4 · Dashboard 首页重排（hero 分区）
- `dash-hero` 拆为**三个视觉层级**：状态大号字（终端 READY/警告色）+ 三 KPI stat block + 状态徽章流
- 今日用量三卡 → **单行大数字 stat band**，metric icon 底色拉开；数字用 mono + `clamp()` 响应式
- 24h 小时图保留但**重配色**：chart-1/2/3 在深色下换成高反差橙/青/玫红
- 7 天 heatmap 保持结构，换成深色系 heat 渐变

### Lever 5 · 动效层（克制但明显）
- 进场：页面卡片 `translateY(6px)→0` + `opacity` 交错 `stagger`（150ms 间隔）
- hover：卡片轻微 `translateY(-1px)` + border 高亮；按钮 `:active` 物理按压
- 数字滚动计数 / chart 条高度过渡（已部分有，统一 duration + easing）
- **`prefers-reduced-motion` 全降为 0**（已有，维持）

### Lever 6 · 全局一致性打磨
- **icon 统一**：`web/js/icons.js` 全面检查，统一 stroke=1.5，同一 icon 家族
- 空态 / loading / error 状态重做：不是"灰色 !"而是**带插画的卡片空态** + **骨架屏**（替代转圈）
- 表头、badge、tag、modal、toast 圆角/描边/阴影统一，shape lock

---

## 4. 改动边界 (红色底 · 永不静默改)

| 项目 | 状态 |
|------|------|
| 路由 slug / 页面切换逻辑 | **不变**（只改 CSS class 与 template 结构，不换 key） |
| 导航标签文案 | **不变**（运行总览/账号管理/...全保留） |
| 表单字段名 + 顺序 | **不变** |
| 品牌文字 / wordmark | 沿用 "Buddy 2 API"，仅改排版呈现 |
| 后端 API / data 契约 | **零改动**（纯前端） |
| 语言 | 中文 UI，**零 em-dash** |

---

## 5. 风险与验证

- **回归安全**：每个 lever 后跑 `pytest` + `tests/test_web_assets.py`（ESM 语法校验）确保没打断页面 JS
- **移动端**：rail→底部 tab + 单列 collapse（`< 760px` 已有多数规则）
- **对比度**：dark 语义色逐项按 WCAG AA 复核
- **screenshot**：改完用 Playwright 截本地页面供你对比（我不能看图，但会存文件路径给你开）

---

## 6. 执行节奏（Stage 6 每 lever 一 commit）

| 顺序 | Lever | 产出 |
|------|-------|------|
| 1 | 主题系统 + 深色底 | 双主题 token，页面整体换肤 |
| 2 | 字体自托管 + mono 强化 | fonts/ + @font-face + 数字 tabular |
| 3 | rail 侧边栏布局 | Grid 两分区，导航重建 |
| 4 | dashboard hero 重排 + 图表配色 | 首页视觉冲击 |
| 5 | 动效层 | 进场/交错/hover |
| 6 | 一致性打磨 | icon/空态/骨架屏/形状 |

每步：实现 → pre-flight 自检 → 本地截图 → commit + push → 你巡视抽样确认。