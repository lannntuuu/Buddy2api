# 状态按钮颜色区分:通道启停 vs 凭证启停 Spec

- 分支:`feature/bailian-provider`(延续)
- 状态:待实现
- 用户需求:通道列表的「停用/启用」按钮与凭证列表(账号表)的「禁用/启用」按钮,当前都是无色 `btn s`,视觉上无法区分层级——给它们各自固定的语义色,一眼分辨两类操作。

## 1. 现状核实(实现依据)

- **通道按钮**(channels.js 列表两组):`class="btn s" :class="c.enabled?'warn':'ok'"` —— **`.btn.ok` / `.btn.warn` 在 app.css 中不存在**(CSS 里只有 `.btn.danger/.pri/.s`),所以颜色类是空挂,实际渲染无色。这是 bug 性质的遗留。
- **凭证按钮**(Card D 账号表):`class="btn s"` 无色,文案 `禁用/启用` 按 `a.status` 切换。
- CSS 变量齐全:`--ok/--ok-bg/--ok-border`(绿系)、`--warn/--warn-bg`(黄系)双主题均有定义,新增按钮样式可直接用。

## 2. 改动清单(仅 channels.js + app.css)

### 2.1 app.css 新增两个按钮色
```css
.btn.ok{background:var(--ok-bg);color:var(--ok);border-color:var(--ok-border)}
.btn.ok:hover{background:var(--ok);color:#fff;border-color:var(--ok)}
.btn.warn{background:var(--warn-bg);color:var(--warn);border-color:var(--accent-border)}
.btn.warn:hover{background:var(--warn);color:#fff;border-color:var(--warn)}
```
(暗色主题自动生效——变量双主题已定义;hover 用实底反白与 `.btn.danger:hover` 的既有模式一致。)

### 2.2 语义分配(核心:两处按钮颜色必须不同)
- **通道列表「启用/停用」**(影响整个通道的调度):保持现有 `enabled?'warn':'ok'` 动态——启用中显示黄「停用」(警示动作),停用中显示绿「启用」(恢复动作)。CSS 类补上后自然生效。
- **凭证表「禁用/启用」**(单个账号行):改**中性描边不参与红绿语义**,避免与通道按钮混淆——加 class `:class="a.status==='active'?'':'plain'"`?更简单方案:凭证按钮保持无色 `btn s` 不变,但**文案与 hover 强化**;由于用户明确要求"颜色需要不同",采用:凭证按钮用**中性灰描边**(新增 `.btn.plain{border-color:var(--border-strong);color:var(--fg-2)}` + hover 变 `var(--fg)`),与通道的红/绿形成三层:通道=彩色语义按钮,凭证=灰描边普通按钮。
- 结论:通道按钮红/绿(warn/ok),凭证按钮灰描边(plain);同屏并排时层级清晰——通道是"影响调度的大开关",凭证是"行级小操作"。

### 2.3 范围
- 只动这两处按钮;danger(删除)等其它按钮不动;Card D 里「测试/刷新/保存」等保持 `btn s`。

## 3. 校验与验收
- node --check channels.js;check_roots2 全页面 OK;pytest tests/test_web_assets.py tests/test_docs_encoding.py tests/test_custom_channels.py tests/test_custom_channels_gmi.py tests/test_custom_channels_bailian.py -q -p no:cacheprovider($env:PYTHONPATH="$PWD\src");
- 验收:
  - [ ] `.btn.ok`/`.btn.warn`/`.btn.plain` 在 app.css 有定义且双主题生效;
  - [ ] 通道按钮:启用中=黄「停用」,停用中=绿「启用」(两组一致);
  - [ ] 凭证按钮:灰描边,active 行显示「禁用」,inactive 行显示「启用」;
  - [ ] 两类按钮同屏颜色明显不同;
  - [ ] 回归通过。

## 4. Out of Scope
- 不改任何交互逻辑(toggleChannel/toggle 契约不变);不新增图标;其它页面按钮不动。
