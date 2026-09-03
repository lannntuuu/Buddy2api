# Stage 4 Brand Tokens + Anti-Slop Audit

## Tokens（Overhaul 终版）

### Color（亮色单主题）

```css
:root{
  /* 基底 */
  --bg:#fafaf8;            /* 页面底，暖白（沿用现行基调，去除纯白刺眼感） */
  --bg-elevated:#ffffff;   /* 卡片 / 顶栏 */
  --bg-sunken:#f5f5f2;     /* 工具条 / 表头 / 内嵌块 */

  /* 文本 */
  --fg:#1d1c1a;            /* 主文本（原深顶栏色转为正文色，保留品牌记忆） */
  --fg-2:#6e6d67;          /* 次级 */
  --fg-3:#9e9b93;          /* 弱化 / 占位 */

  /* 边框 */
  --border:#e4e3de;
  --border-strong:#d4d3cd;

  /* 品牌 accent（唯一强调色，沿用现橙） */
  --accent:#e85d13;
  --accent-strong:#c9480d;
  --accent-soft:#fff0df;
  --accent-border:#f3c096;

  /* 语义 */
  --ok:#0a8a4a;   --ok-bg:#e8f5ee;
  --warn:#9a5c14; --warn-bg:#fff1da;
  --err:#c03434;  --err-bg:#fdeeee;

  /* 图表（三通道区分 + 热力阶 + 状态） */
  --chart-1:#d85410; --chart-1-soft:#fff0df;   /* requests */
  --chart-2:#14755f; --chart-2-soft:#e8f5f0;   /* tokens */
  --chart-3:#45566a; --chart-3-soft:#edf1f5;   /* credit */
  --heat-0:#e8e9e6; --heat-1:#fff0ce; --heat-2:#ffc98f;
  --heat-3:#ff9855; --heat-4:#f15f14; --heat-5:#3f403e;
  --cache-ok:#0a8a4a; --cache-partial:#c47f00; --cache-approx:#b04040; --cache-empty:#9e9b93;
}
```

规则：
- 全文件禁止裸 hex（codeblk 深色块除外，其自身是一组令牌：--code-bg #242321 等）
- --blue 系变量删除（现为别名，直接换用 accent 名）
- 深色只允许出现在 codeblk 与 toast.info

### Type

| 令牌 | 值 | 用途 |
|---|---|---|
| --font | 现有系统栈不变 | 正文 |
| --mono | 'SF Mono','Fira Code',Consolas,'Noto Sans Mono',monospace | 数字 / Key / 模型名 / 代码 |
| --fs-xs 10px / --fs-s 11px / --fs-b 12px（基准）/ --fs-m 13px / --fs-l 15px / --fs-h 18px / --fs-xl 22px / --fs-hero 28px | | 页面标题 22，卡片标题 15，正文 12/13，轴标签 10 |

### Spacing

| 令牌 | 值 |
|---|---|
| --sp-1..6 | 4 / 8 / 12 / 16 / 24 / 32px |
| 内容区 | max-width 1600px，padding 18px（移动 10px），沿用 |
| 卡片内边距 | 16px 统一（现 15/16 混用 → 16） |

### Radius（单一体系，三档）

| 令牌 | 值 | 用途 |
|---|---|---|
| --r-s | 4px | badge / tag / 热力格 |
| --r-m | 6px | 按钮 / 输入 / nav / codeblk / 分段控件 |
| --r-l | 8px | card / modal / 顶栏元素 |

### Motion

| 令牌 | 值 |
|---|---|
| --dur-fast | 120ms（hover 反馈） |
| --dur-base | 180ms（进出场 / 面板） |
| --ease | cubic-bezier(0.2, 0, 0, 1) |
| 动效仅 transform / opacity | 无 scroll listener，无 JS 动画 |

## LILA Rule override check

不适用：品牌色为橙（#e85d13），非紫/紫罗兰。accent 保留原值即品牌忠实。

## Anti-Slop Cleanup Checklist

| 项 | 判定 | 说明 |
|---|---|---|
| em-dash 可见面 | clean | 现模板用「·」与「——」仅中文行文处；交付物内统一避免 |
| section-number eyebrows | clean | 不存在 |
| AI-purple / mesh 渐变 | clean | 不存在；新设计也不引入 |
| 三等卡 feature 区 | keep | today-metrics 三联是数据指标卡（功能必需），非营销卡，保留但令牌化 |
| div 假产品 UI | clean | 不存在（图表是真实数据） |
| scroll cue | clean | 不存在 |
| locale strip | clean | 不存在 |
| hero 版本标签 | **clean** | `brand-meta "Local model gateway · v2.1.0"` 保留形式但改为动态注入（审计 #12） |
| hero 底部装饰文字条 | clean | 不存在 |
| photo-credit 装饰 | clean | 不存在 |
| 图片上覆盖 pills | clean | 无图片 |
| 填充轨道评分条 | keep | progress 条是真实用量展示，保留 |
| Acme/Nexus 占位名 | clean | 不存在 |
| Jane/John Doe | clean | 不存在 |
| 99.99% 假数 | clean | 全部数据来自 /admin/* 实时接口 |
| Lucide 默认图标 | keep | icons.js 为自绘 15 枚 SVG，非 Lucide；统一 stroke-width 1.5 即可 |
| Inter 默认字体 | keep | 系统字体栈，本机工具合理，无外部字体请求符合断网目标 |
| 100vh hero | clean | 无 hero；layout 用 min-height:100vh 于根容器，合规 |
| h-screen + flex 百分比 | clean | 现用 grid 模板列，合规 |

结论：无 AI slop 存量需要清除；需处理的是"版本号写死"这一真实 bug 与令牌失控。清单中 4 项 keep 均为功能性保留，有明确理由。

## 实施约束（进入 Stage 5 前锁定）

- 无构建步骤红线保留：不引入 Vite/打包器，原生 ESM + 原生 CSS
- Vue 3 global build 本地化后仍走 CDN 版本号 3.4.21 对应的 vendor 文件，不升级大版本（避免行为回归）
- 所有色值经由 :root 令牌；JS 里清除内联 hex（dashboard.js heatStyle / cacheStatusColor 改用 CSS class + var）
