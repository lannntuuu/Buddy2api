# Overhaul 视觉重做 · 可执行实施规范 (EXECUTION SPEC)

> 本文件是**自包含、可直接由 subagent 执行**的实施方案。subagent **不需要回看主对话**。
> 只做视觉层。改文件：`web/css/app.css`、`web/index.html`、`web/js/app.js`、`web/js/icons.js`、各 `web/js/pages/*.js` 的 template 结构。**禁止碰** `storage/`、`gateway/`、后端路由、data/sqlite。
> 红线：路由 slug、导航文案、表单项名/顺序、后端 API、中文里**零 em-dash (——)**。

---

## 0. 总目标

让用户肉眼一秒看出是"新设计"。方向：**深色终端工程感**（Linear / Vercel / Warp 一族），强橙 accent + 等宽数字。**浅色是默认主皮肤**，深色是好切换的用户偏好。

### 实施必读（本规范所有词都按这些步骤执行，先 READ 再看下一步）

**必须按序执行 6 个 Lever。每个 Lever 结束必须：**
1. 达成本 Lever 所有改动
2. 跑 `pytest`（工作目录根）
3. 跑 ESM 语法校验（见 §9 验证章节）
4. `git add` 涉及文件 → `git commit`（message 见 §10）→ `git push`
5. 更新 `redesign-audit/05-implementation-log.md`（§11）

**不做完上一个 Lever 不进入下一个。每个 Lever 一颗 commit。**

---

## 1. 现状核对（必须先看，避免改错 class 名）

| 文件 | 内容 | 关键 class |
|------|------|-----------|
| `web/index.html` | 15 行 SPA 外壳 | `#app`、引 `/static/css/app.css` |
| `web/css/app.css` | 663 行单层 token | 全部：`--bg/--fg/--accent/...`、`.topbar/.brand-*/ .topnav/.nav-item/.main/.content/.card/.card-h/.card-p/.dash-hero/.dash-grid/.today-*/.metric/.phead/.tbar/.btn/...` |
| `web/js/app.js` | 外壳组件 | `layout`(class)= 顶栏 topbar + main.content + toasts；`nav` 数组 9 项；每次切换 `go(k)` |
| `web/js/icons.js` | 14 个手写 SVG | stroke-width=`1.8` |
| `web/js/pages/*.js` | 9 个页面独立组件 | 各自 `class="phead"` 标题、`class="card"` 卡片、`class="tbar"` 工具条、表格 |

**上一轮已定的浅色 token（保留，是浅色主皮肤底色）：** 见 app.css 顶部 `:root{}`（`--bg:#fafaf8`、`--accent:#e85d13` 橙、`--fs-b:12px` 等）。

---

## 2. LEVER 1 · 双主题系统（默认浅色 + 可切深色 + 记忆 + 防闪烁）

**目标**：页面能切深色且默认浅色，切换记忆在 localStorage，首帧不闪。深色是"终端式暖黑底 + 橙/青/玫红五行色"。

### 2.1 `web/index.html`：加防闪烁注入脚本 + 初始化 data-theme

在 `<head>` 的 `<link rel="stylesheet">` **之前**插入：

```html
<script>
  // Buddy2API theme bootstrap (no flash)
  (function(){
    var saved=null;
    try{saved=localStorage.getItem('cb_gw_theme')}catch(_){}
    var valid=(saved==='light'||saved==='dark');
    var mode=valid?saved:'light'; /* LS 无值 => light 默认 */
    if(valid)document.documentElement.setAttribute('data-theme',mode);
    document.documentElement.style.backgroundColor=mode==='dark'?'#0d0c0b':'#fafaf8';
  })();
</script>
```

### 2.2 `web/css/app.css`：把浅色 token 移进 `:root[data-theme="light"]`，新增深色块

- 现有 `:root{...}` 的色值**保持不变**，但把整块选择器改成 `:root[data-theme="light"]`（保留继承给子元素）。
  - 注意：**字号/间距/圆角/动效 token 不属于主题**，留在 `:root{}` 不变，只把**颜色类**拆进 `[data-theme]` 块。
- 在下半部追加 `:root[data-theme="dark"]{ ... }`，深色值如下（对照浅色一一替换颜色 token）：

```css
:root[data-theme="dark"]{
  /* color · base */
  --bg:#0d0c0b;
  --bg-elevated:#161412;
  --bg-sunken:#1f1c19;
  --fg:#e8e4de;
  --fg-2:#a7a29a;
  --fg-3:#6f6a63;
  --border:#2a2622;
  --border-strong:#3a352f;

  /* color · brand accent (same orange, tuned for dark) */
  --accent:#f27626;
  --accent-strong:#ff8b3d;
  --accent-soft:#2a1a0e;
  --accent-border:#5a3a1c;

  /* color · semantic (dark-tuned) */
  --ok:#35c07a;  --ok-bg:#10201a;  --ok-border:#1d4435;  --ok-fg:#6fe0a6;
  --warn:#e2a64a;  --warn-bg:#241a0e;  --warn-fg:#ffc97a;  --warn-fg-strong:#ffc97a;
  --err:#e56767;  --err-bg:#241212;  --err-border:#4a2222;

  /* color · charts (dark-tuned, high contrast) */
  --chart-1:#f27626;  --chart-1-soft:#2a160a;
  --chart-2:#37b0a0;  --chart-2-soft:#0e211f;
  --chart-3:#e34f8f;  --chart-3-soft:#23101a;
  --heat-0:#20201e; --heat-1:#2e250f; --heat-2:#4a3413;
  --heat-3:#7a4d1a; --heat-4:#d4630f; --heat-5:#3f403e;
  --cache-ok:#35c07a; --cache-partial:#d8a13a;
  --cache-approx:#e56767; --cache-empty:#6f6a63;

  /* code block (stays dark in both) */
  --code-bg:#060605; --code-fg:#d8d4cc;
  --code-border:#2a2622; --code-kw:#ff8a48; --code-str:#ffd19a;
}
```

> **注意 dark 里无 `--warn-bg` 时 CSS 会把浅色值覆盖**;本块补了。凡是浅色 `:root` 里出现但你在 dark 里没定义的颜色，dark 下会继承浅色错误值 → **不要把任何颜色漏掉**。逐 pair 核对：bg/elevated/sunken、fg/fg-2/fg-3、border/border-strong、accent 全家、ok/warn/err 全家、chart 全家、heat 0-5、cache 全家、code 全家。

### 2.3 `web/css/app.css`：追加「硬编码色 / 白字」适配

深色下有几处写死 `#fff`（按钮文字、`.seg button.on`、`.btn.pri`、`.today-value` 等），这些在浅色OK、深色也OK（深色按钮底仍够黑）。**但需要新增**全局补丁：

```css
/* theme-aware scrollbars + selection (optional polish) */
[data-theme="dark"] ::selection{background:var(--accent-soft);color:var(--fg)}
/* ensure topbar/surface read as elevated in dark */
[data-theme="dark"] .topbar{background:var(--bg-elevated)}
[data-theme="dark"] .card{background:var(--bg-elevated)}
[data-theme="dark"] th{background:var(--bg-sunken)}
```

（大部分已用 `var(--bg-elevated)` 引用，实际会自翻转；这几条只是确保残存硬编码被覆盖。实施时 grep 查 `#fff`、`#000`、`rgba(29,28,26` 在 app.css 的残留并逐一用 token 或加 dark 覆盖。）

### 2.4 切换器 UI：放顶栏右侧（浅/深开关）

`web/js/app.js` 外壳：
- 加一个 `theme=ref(localStorage.getItem('cb_gw_theme')||'light')`；
- `toggleTheme(){ theme.value=theme.value==='dark'?'light':'dark'; document.documentElement.setAttribute('data-theme',theme.value); try{localStorage.setItem('cb_gw_theme',theme.value)}catch(_){} }`
- template 顶栏 `.top-actions` 里、刷新按钮旁加一个文字切换钮：

```html
<button class="iconbtn" @click="toggleTheme" :title="theme==='dark'?'切到浅色':'切到深色'">
  <span v-html="theme==='dark'?I.sun:I.moon"></span>
</button>
```

`icons.js` 新增 `sun`、`moon` 两个 SVG（stroke-width=1.8，与现有一致）：
- `sun`: 圆 + 8 条射线
- `moon`: 新月 path

### 2.5 `.iconbtn` 样式（app.css 追加，浅色深色通用）

```css
.iconbtn{height:36px;min-width:36px;padding:0 10px;border:1px solid var(--border-strong);
  background:var(--bg-elevated);color:var(--fg-2);border-radius:var(--r-m);
  display:inline-flex;align-items:center;justify-content:center;cursor:pointer;
  transition:background var(--dur-fast) var(--ease),color var(--dur-fast) var(--ease),border-color var(--dur-fast) var(--ease)}
.iconbtn:hover{background:var(--bg-sunken);color:var(--fg)}
.iconbtn svg{width:16px;height:16px}
```

**Lever 1 完成判据**：默认打开浅色；点切换钮变深色；刷新后保持；无首帧闪烁；`pytest`+ESM 校验通过。

---

## 3. LEVER 2 · 字体自托管 + mono 数字强化

### 3.1 字体来源（必须自托管，禁外链 Google Fonts）

Geist 和 Geist Mono 字体文件（woff2）需放入 `web/fonts/`。若无网络可离线获取，**回退方案**：用系统等宽 + sans 组合并在 `@font-face` 注释说明。**优先**尝试下载 Geist / Geist Mono woff2（一次 fetch 获取留存）。文件名例如：
- `web/fonts/Geist-Regular.woff2`、`Geist-Medium.woff2`、`Geist-SemiBold.woff2`、`Geist-Bold.woff2`
- `web/fonts/GeistMono-Regular.woff2`、`GeistMono-Medium.woff2`、`GeistMono-SemiBold.woff2`

### 3.2 `web/css/app.css`：`@font-face` 追加（在 `:root` 前）

```css
@font-face{font-family:'Geist';src:url('/static/fonts/Geist-Regular.woff2') format('woff2');font-weight:400;font-display:swap}
@font-face{font-family:'Geist';src:url('/static/fonts/Geist-Medium.woff2') format('woff2');font-weight:500;font-display:swap}
@font-face{font-family:'Geist';src:url('/static/fonts/Geist-SemiBold.woff2') format('woff2');font-weight:600;font-display:swap}
@font-face{font-family:'Geist';src:url('/static/fonts/Geist-Bold.woff2') format('woff2');font-weight:700;font-display:swap}
@font-face{font-family:'Geist Mono';src:url('/static/fonts/GeistMono-Regular.woff2') format('woff2');font-weight:400;font-display:swap}
@font-face{font-family:'Geist Mono';src:url('/static/fonts/GeistMono-Medium.woff2') format('woff2');font-weight:500;font-display:swap}
@font-face{font-family:'Geist Mono';src:url('/static/fonts/GeistMono-SemiBold.woff2') format('woff2');font-weight:600;font-display:swap}
```

`font-display:swap` 保首绘。若无 woff2 下载到：直接用字重分支回退，或注释「未 vendor，用 system mono 替代」，不阻断。

### 3.3 更新 `--font` / `--mono`

- `:root` 里 `--font` 改为 `'Geist',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans SC',sans-serif`
- `--mono` 改为 `'Geist Mono','SF Mono','Fira Code',Consolas,'Noto Sans Mono',monospace`

数字等宽强化（所有统计数字用 mono + tabular）：
- 在 app.css 追加大范围开关：

```css
.m-value,.today-value,.health-kpi .v,.metric .m-value,.pkg-kpi .v,
.pkg-time,.credit-rem,.official-val,.rank-value,.hour-chart-meta strong,
.state-number,table td{font-family:var(--mono);font-feature-settings:'tnum' 1}
```

> 不强求改每个页面 JS 去拆 tabular；用这条 CSS 匹配已有 class 即可。合法、零 JS 改动。

**Lever 2 完成判据**：`web/fonts/` 存在 woff2（或回退已注明）、`@font-face` 生效、页面数字走等宽不跳动。

---

## 4. LEVER 3 · Rail 侧边栏布局重建

### 4.1 目标
顶栏一排 icon chip → **左侧 56px 竖向 rail 侧边栏**（深色底），Grid 两分区 `grid-template-columns:56px 1fr`。激活态 = 左侧竖条 + icon 变色（终端 `>` 提示符感）。

### 4.2 `web/js/app.js` 外壳 template 重写（关键）

将顶栏品牌区移到 rail 顶部，导航移到 rail 内竖向；右上角保留 meta + 主题切换 + 刷新。

```html
<div class="shell">
  <aside class="rail">
    <div class="rail-brand" v-html="I.logo"></div>
    <nav class="railnav">
      <div v-for="n in nav" :key="n.k" class="rail-item" :class="{on:page===n.k}"
           @click="go(n.k)" :title="n.l" v-html="n.i"></div>
    </nav>
    <div class="rail-foot">
      <button class="rail-icon" @click="toggleTheme" :title="theme==='dark'?'切到浅色':'切到深色'" v-html="theme==='dark'?I.sun:I.moon"></button>
    </div>
  </aside>
  <div class="shell-body">
    <div class="shell-head">
      <div class="shell-title">{{meta.title}}<span class="shell-ver" v-if="meta.version"> v{{meta.version}}</span></div>
      <div class="shell-actions">
        <span class="tag">{{metaTag}}</span>
        <button class="refresh-cta" @click="hardRefresh"><span v-html="I.refresh"></span><span>刷新</span></button>
      </div>
    </div>
    <main class="main"><div class="content" v-for="...">... 各页 .content 原样 ...</div></main>
  </div>
</div>
```

> **注意**：`nav` 数组元素 `{k,l,i}` 不变（key 不变 → 路由/导航标签红线满足）。只是**渲染位置/样式**变。移动端 `go(k)`、`localStorage cb_gw_page` 全保留不变。

### 4.3 icons.js 新增 `logo`

`logo`：一个 24x24 方块内 "B2" 或终端 `>` 符号的 SVG（stroke-width=1.8）。给 rail 顶部品牌用。

### 4.4 `web/css/app.css`：新增 shell layout 布局（替换原 topbar 模式）

删/注释旧 `.topbar`、`.topnav`、`.nav-item`（或保留顶栏仅作移动端 fallback；建议**重写为 rail**并在 media 里处理移动端）。新增：

```css
.shell{display:grid;grid-template-columns:56px minmax(0,1fr);min-height:100vh;background:var(--bg);color:var(--fg)}
.rail{display:flex;flex-direction:column;align-items:center;gap:6px;
  background:var(--bg-sunken);border-right:1px solid var(--border);padding:10px 0;
  position:sticky;top:0;height:100vh;z-index:500}
.rail-brand{margin-bottom:10px;color:var(--accent);width:32px;height:32px;
  display:grid;place-items:center}
.rail-brand svg{width:26px;height:26px}
.railnav{display:flex;flex-direction:column;gap:4px;flex:1}
.rail-item{width:40px;height:40px;display:grid;place-items:center;color:var(--fg-3);
  border-radius:var(--r-m);cursor:pointer;position:relative;
  transition:background var(--dur-fast) var(--ease),color var(--dur-fast) var(--ease)}
.rail-item svg{width:19px;height:19px}
.rail-item:hover{color:var(--fg);background:var(--bg-elevated)}
.rail-item.on{color:var(--accent);background:var(--accent-soft)}
.rail-item.on::before{content:'';position:absolute;left:-8px;top:50%;transform:translateY(-50%);
  width:3px;height:22px;border-radius:2px;background:var(--accent)}
.rail-foot{display:flex;flex-direction:column;gap:4px}
.rail-icon{width:36px;height:36px;display:grid;place-items:center;color:var(--fg-3);
  background:none;border:none;cursor:pointer;border-radius:var(--r-m);
  transition:background var(--dur-fast) var(--ease),color var(--dur-fast) var(--ease)}
.rail-icon:hover{background:var(--bg-elevated);color:var(--fg)}
.rail-icon svg{width:18px;height:18px}
.shell-body{min-width:0;display:flex;flex-direction:column;min-height:100vh}
.shell-head{position:sticky;top:0;z-index:400;min-height:52px;padding:8px var(--sp-5);
  display:flex;align-items:center;justify-content:space-between;gap:var(--sp-3);
  background:color-mix(in srgb,var(--bg) 82%,transparent);
  backdrop-filter:blur(8px);border-bottom:1px solid var(--border)}
.shell-title{font-weight:800;font-size:var(--fs-l);display:flex;gap:8px;align-items:baseline}
.shell-ver{color:var(--fg-3);font-size:var(--fs-s);font-weight:500;font-family:var(--mono)}
.shell-actions{display:flex;align-items:center;gap:var(--sp-2)}
```

### 4.5 移动端（新增在现有 `@media(max-width:760px)` 内或独立段）

- rail 从左侧 56px → 底部 tab 条：shell 改 `grid-template-columns:1fr;grid-template-rows:1fr 56px`。
- `.rail`：`position:fixed;left:0;right:0;bottom:0;top:auto;height:56px;width:100%;flex-direction:row;justify-content:space-around;border-right:none;border-top:1px solid var(--border);padding:0}`
- `.rail-brand,.rail-foot` 隐藏；`.railnav{flex-direction:row;width:100%;justify-content:space-around}`；`.rail-item.on::before{left:50%;right:auto;top:-4px;transform:translateX(-50%);width:22px;height:3px}`
- `.shell-head` 继续 sticky top 52px。

**Lever 3 完成判据**：桌面左侧 56px rail + 顶部细条 head；激活项左侧橙竖条；移动端 rail 沉底。导航 key/文案不变，`go` 仍工作。

---

## 5. LEVER 4 · Dashboard Hero 重排 + 图表深色重配色

### 5.1 `dashboard.js` hero 结构轻度重组（只改 template class，不改数据/逻辑）

现有 `dash-hero`（.health-main + 3 KPI）保留数据绑定，改成更多视觉层级。目标 template 骨架（围绕现有 `s`/`healthText()`/`n()`/`pct()`/`ms()` 等字段）：

```html
<div class="dash-hero">
  <div class="hero-left">
    <div class="hero-statusline">
      <span class="health-dot" :class="healthClass()"></span>
      <span class="hero-state">{{healthText()}}</span>
    </div>
    <div class="hero-sub">本机 OpenAI 兼容网关 · {{s.active_accounts}}/{{s.total_accounts}} 账号可用 · {{s.active_keys}}/{{s.total_keys}} Key 可用</div>
    <div class="status-line">
      <span class="badge" :class="s.active_accounts?'ok':'err'">Accounts {{s.active_accounts}}</span>
      <span class="badge" :class="s.active_keys?'ok':'err'">Keys {{s.active_keys}}</span>
      <span class="badge" :class="s.today?.errors?'err':'ok'">Errors {{s.today?.errors||0}}</span>
      <span class="tag">Filtered {{s.today?.filtered||0}}</span>
    </div>
  </div>
  <div class="hero-kpis">
    <div class="hero-kpi"><div class="k">今日请求</div><div class="v state-number">{{n(s.today?.requests)}}</div></div>
    <div class="hero-kpi"><div class="k">今日成功率</div><div class="v state-number">{{pct(s.today?.success_rate)}}</div></div>
    <div class="hero-kpi"><div class="k">平均耗时</div><div class="v state-number">{{ms(s.today?.avg_duration_ms)}}</div></div>
  </div>
</div>
```

新增样式（app.css）：

```css
.dash-hero{grid-template-columns:minmax(0,1.4fr) minmax(340px,.6fr);gap:var(--sp-5);
  padding:var(--sp-5) var(--sp-6);background:var(--bg-elevated);
  border:1px solid var(--border);border-radius:var(--r-l);margin-bottom:var(--sp-4);
  display:grid;align-items:center;position:relative;overflow:hidden}
.dash-hero::after{content:'';position:absolute;right:-120px;top:-120px;width:260px;height:260px;
  border-radius:50%;background:radial-gradient(circle,var(--accent-soft),transparent 70%);
  pointer-events:none;opacity:.5}
.hero-statusline{display:flex;align-items:center;gap:var(--sp-2);margin-bottom:6px}
.hero-state{font-size:var(--fs-xl);font-weight:900;color:var(--fg);letter-spacing:-.01em}
.hero-sub{font-size:var(--fs-b);color:var(--fg-2);line-height:1.6;margin-bottom:14px}
.hero-kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:var(--sp-2);
  border:1px solid var(--border);border-radius:var(--r-m);overflow:hidden;
  background:var(--bg-sunken)}
.hero-kpi{padding:var(--sp-3);border-right:1px solid var(--border);min-width:0}
.hero-kpi:last-child{border-right:0}
.hero-kpi .k{font-size:var(--fs-xs);color:var(--fg-3);text-transform:uppercase;letter-spacing:.2px}
.hero-kpi .v{font-size:var(--fs-l);font-weight:800;margin-top:5px}
```

（把 `.hero-kpi .v` 加入 §3 的 mono 列表。现有 `.health-main/.health-kpis` 可留作复用或删。程序用到的 `healthClass/healthText/n/pct/ms` 全部保留。）

### 5.2 今日用量三卡、24h 图、heatmap：深色配色已由 token 自翻转

Lever 1 的 `--chart-*`、`--heat-*`、`--cache-*` 深色值已覆盖，无额外 JS 改动。
仅需确认 `.today-value` 用 mono 大号（已有 `--fs-hero`），并在 CSS 补 `.today-value{font-feature-settings:'tnum' 1}`。

**Lever 4 完成判据**：hero 有状态大字 + 三 KPI 块 + 装饰光斑；图表在深色下呈橙/青/玫红对比；数字等宽。

---

## 6. LEVER 5 · 动效层（克制但明显）

全部用 CSS transition/keyframes，禁框架、禁 `window.scroll`。`prefers-reduced-motion` 已克制（app.css 已有 `@media(prefers-reduced-motion){...}` 全局 0）。

### 6.1 进场 stagger（每页内容卡进入）

在 `.content` 下所有直子卡加延入。用 selector 匹配常见卡片 class：`.content > .card, .content > .dash-hero, .content > .today-usage, .content > section, .content > .dash-grid`。

```css
.content > .card,.content > .dash-hero,.content > .today-usage{animation:riseIn var(--dur-base) var(--ease) both}
.content > .dash-hero{animation-delay:.04s}
.content > .today-usage{animation-delay:.08s}
.content > .dash-grid{animation:riseIn var(--dur-base) var(--ease) both;animation-delay:.12s}
@keyframes riseIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
```

> 页面切换（`v-if` 切组件）会每次触发，viewport 内的卡片统一 stagger，多次切换仍 OK。无需 JS。

### 6.2 hover / 物理按压

```css
.card:hover{box-shadow:0 4px 20px rgba(0,0,0,.06)}
.btn:active,.iconbtn:active,.refresh-cta:active,.rail-item:active{transform:scale(.97)}
```

### 6.3 数字滚动（可选，减脂）

不强求。若做：dashboard 今日请求大数字由 `{{n(s.today?.requests)}}` 保持即可，不引库。**跳过合法**，避免复杂度。

**Lever 5 完成判据**：切页卡片逐个淡入上移；hover 微阴影；按压缩放；reduced-motion 下全关。

---

## 7. LEVER 6 · 一致性打磨

### 7.1 空态/error 轻量升级（不动数据逻辑）

现 `.empty` 是灰字 + `em`。改为更克制但仍明显的占位（纯 CSS）：

```css
.empty{text-align:center;padding:48px var(--sp-4);color:var(--fg-3);
  display:flex;flex-direction:column;align-items:center;gap:var(--sp-2)}
.empty::before{content:'';width:40px;height:40px;border-radius:12px;
  background:var(--bg-sunken);border:1px solid var(--border);display:block;
  background-image:radial-gradient(circle at center,var(--fg-3) 1.5px,transparent 1.5px);
  background-size:8px 8px}
.empty .em{margin-bottom:0}
```

（保留 `.empty .em` 的 emoji/text，只是加了个网格点占位块。`style="..."` 内联残留不强制清。）

### 7.2 loading 骨架（替代转圈可选）

现 `.load .spin` 是全站 loading。保留 spin 作为通用，仅在 dashboard `v-if="ld"` 处可换骨架。**不强求**；若时间够，把 `.load` 改 flex 居中+文案即可。合法跳过。

### 7.3 icon 检查：stroke 统一 + `sun/moon/logo` 补全

- 确认所有 `web/js/icons.js` svg `stroke-width="1.8"` 一致；新增 `sun/moon/logo` 也 1.8。
- 下一步（跨越 theme/rail/hero 已引入），补 `logo`。

### 7.4 shape / shadow 一致性

深色下 `.card`、`.modal`、`.toast` 阴影如需更柔和可用：
```css
[data-theme="dark"] .card{box-shadow:none}
[data-theme="dark"] .modal{box-shadow:0 24px 80px rgba(0,0,0,.6)}
[data-theme="dark"] .toast{box-shadow:0 10px 30px rgba(0,0,0,.5)}
```

**Lever 6 完成判据**：空态有网格点占位；icon stroke 统一 1.8；阴影深色柔和；无 em-dash。

---

## 8. 交付后自检 (Hard Pre-Flight)

- [ ] em-dash 检查：`git grep -n "—"` 在 `web/` 应为 0（中文文案与注释无 U+2014）
- [ ] 主题 LABEL 一条：只 1 个 accent（橙），全页一致
- [ ] 深色下无白字白底 / 无低对比按钮（`.btn.pri` 白字橙底 OK；检查语义 badge）
- [ ] `data-theme` 默认浅色，LS 无值时浅色
- [ ] rail 移动端沉底、`prefers-reduced-motion` 生效
- [ ] 零 em-dash、零新增外链字体

---

## 9. ESM 语法校验（每个 Lever 后必跑，因为 vue 页面是 ES module）

现有测试文件 `tests/test_web_assets.py` 已有 ESM 语法校验。跑法：

```bash
pytest -q tests/test_web_assets.py
```

再加手动 Node ESM parse（若 Node 可用）：
```bash
node --input-type=module -e "import('./web/js/app.js').then(()=>console.log('OK')).catch(e=>{console.error('PARSE FAIL',e);process.exit(1)})"
```

**完整 pytest**（冒烟，quant 无关）：
```bash
pytest -q
```

---

## 10. Commit 纪律（每个 Lever 一颗）

```
Lever1  commit msg:  feat(web): dual theme system (light default) with flash-free bootstrap + dark skin
Lever2  commit msg:  feat(web): self-host Geist/Geist Mono with tabular-nums numbers
Lever3  commit msg:  feat(web): rebuild shell into left 56px rail nav with active state
Lever4  commit msg:  feat(web): restyle dashboard hero into status + KPI band, dark-tuned charts
Lever5  commit msg:  feat(web): tasteful entrance stagger + hover/press motion (reduced-motion safe)
Lever6  commit msg:  feat(web): polish empty/loading states, unify icon stroke, dark shadows
```

每个 commit 前：`git add web/ redesign-audit/05-implementation-log.md`；`git commit -m "..."`；`git push origin refactor/web-console-ia`。

**注意**：当前工作树有未跟踪文件 `config.toml`、`data/backup/credentials.key.latest`。**不要 `git add -A`**，只 add 明确文件，避免误提交这两项。

---

## 11. 实施日志 `redesign-audit/05-implementation-log.md`（每 Lever 追加）

格式（每 Lever 一段）：

```markdown
## Lever N — <name>
- 状态: ✔ done
- 改动文件: web/css/app.css, web/js/app.js, ...
- commit: <hash8> <msg>
- 说明: <2-3 行>
- screenshot: <保存路径，若截屏了>
```

---

## 12. 验证命令汇总 / 兜底

- 主题/展示层面核对：无法截图则用 `git diff --stat` 确认改动文件范围。
- 若 Geist 下载失败：在 `web/fonts/README.md` 写一段说明 + 用 system 回退，Lever 2 仍标记 done（注明回退）。
- 任一 Lever 若因既有代码结构假设不符而做不下去：**停下，不要硬改坏功能**，把差异写进日志并报告，等主对话指示。

> 本规范为自包含执行文档。subagent 照此执行，不向主对话索取额外信息；遇到本规范未覆盖的结构差异，优先用最保守的 CSS 增强实现，避免改动 JS 数据/逻辑。中文文案零 em-dash。