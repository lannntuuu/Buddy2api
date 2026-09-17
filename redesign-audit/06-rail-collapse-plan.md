# Rail 侧栏收缩/展开功能 · 方案 (Lever 7)

> 目标:左侧 56px 纯图标 rail 支持**收缩/展开**。展开时显示每个图标对应的功能全称,收起时回到纯图标窄轨。
> 范围:仅 `web/js/app.js`(外壳 template + state)+ `web/css/app.css`(rail 布局)。**零后端改动、零路由改动、零文案改动**。
> 红线:导航 key/文案不变;中文零 em-dash;只 `git add` 明确文件。

---

## 1. 现状

- `.shell` 是 `grid-template-columns:56px minmax(0,1fr)` 两分区,rail 固定 56px。
- `.rail-item` 只渲染 icon(`v-html="n.i"`),功能名靠 `:title="n.l"` 悬停提示。
- 品牌区 `.rail-brand` 用 `I.logo`;底部 `.rail-foot` 放主题切换。
- 移动端(`<760px`)rail 已沉底为底部 tab 条,icon 无文字。

## 2. 交互设计

- **默认收起**(56px 纯图标),与现状一致,不打扰老用户。
- **展开态**:rail 宽度 56px → **200px**,每个 `.rail-item` 变为 `[icon] + 功能名` 横向排布。
- **切换入口**:rail 底部新增一个**收缩/展开按钮**(双箭头图标),点击切换。
- **状态记忆**:`localStorage` 存 `cb_gw_rail`(`'expanded'`/`'collapsed'`),刷新后保持。
- **移动端**:rail 已沉底为 tab 条,展开态无意义 → 展开态在 `<760px` 下**自动忽略**(保持底部 tab 条)。

## 3. 改动清单

### 3.1 `web/js/app.js`

1. 新增 `railOpen=ref(localStorage.getItem('cb_gw_rail')==='expanded')`
2. 新增 `toggleRail(){ railOpen.value=!railOpen.value; try{localStorage.setItem('cb_gw_rail',railOpen.value?'expanded':'collapsed')}catch(_){} }`
3. template:
   - `<aside class="rail" :class="{open:railOpen}">`
   - `.rail-brand` 内:展开时显示 `{{meta.title}}`(或保留 logo + 文字)
   - `.rail-item` 改为:`<div ... class="rail-item" :class="{on:page===n.k, open:railOpen}"><span class="rail-ic" v-html="n.i"></span><span class="rail-lbl" v-if="railOpen">{{n.l}}</span></div>`
   - `.rail-foot` 底部新增切换按钮:
     ```html
     <button class="rail-icon" @click="toggleRail" :title="railOpen?'收起侧栏':'展开侧栏'" v-html="railOpen?I.chevronL:I.chevronR"></button>
     ```
4. `return{...}` 加 `railOpen,toggleRail`

### 3.2 `web/js/icons.js`

新增两个双箭头 icon(与现有 stroke-width=1.8 一致):
- `chevronL`:`<polyline points="15 18 9 12 15 6"/>`(左双箭头)
- `chevronR`:`<polyline points="9 18 15 12 9 6"/>`(右双箭头)

### 3.3 `web/css/app.css`

1. `.shell` 改为用 CSS 变量控制列宽:
   ```css
   .shell{display:grid;grid-template-columns:var(--rail-w,56px) minmax(0,1fr);...}
   ```
2. `.rail.open` 展开态:
   ```css
   .rail.open{width:200px;align-items:stretch;padding:10px 8px}
   .rail.open .rail-brand{width:100%;justify-content:flex-start;padding:0 10px}
   .rail.open .railnav{align-items:stretch}
   .rail.open .rail-item{width:100%;justify-content:flex-start;padding:0 10px;gap:10px}
   .rail.open .rail-item .rail-lbl{display:inline;font-size:var(--fs-b);font-weight:600;white-space:nowrap}
   .rail.open .rail-item.on::before{left:-8px} /* 激活竖条保持 */
   ```
3. `.rail-lbl` 默认 `display:none`(收起时隐藏)
4. 过渡:`.rail,.rail-item,.rail-brand` 加 `transition:width/...` 平滑;`prefers-reduced-motion` 已全局归零,无需额外处理。
5. 移动端 `<760px`:`.rail.open` 相关展开样式**不生效**(沉底 tab 条逻辑已覆盖),无需额外规则。

## 4. 验证

- 收起默认:刷新后 rail 56px,纯图标,`title` 悬停提示仍在。
- 点底部按钮 → 展开 200px,显示全称;再点 → 收起。
- 刷新后状态保持(localStorage)。
- 移动端(≤760px):rail 沉底 tab 条,展开态不生效。
- `pytest -q tests/test_web_assets.py`(ESM 校验)+ 完整 `pytest -q` 确认无回归。

## 5. 边界与不做的事

- **不做** hover 自动展开(会与点击展开冲突、移动端误触)。
- **不做** rail 内搜索/折叠分组(9 项导航无需)。
- **不改**导航 key、文案、路由、后端。
- **不引入**任何第三方库(纯 CSS + Vue state)。

## 6. Commit

```
feat(web): collapsible rail sidebar with expand/collapse toggle
```
