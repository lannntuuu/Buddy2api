# 前端收尾 · 可执行实施规范 (EXECUTION SPEC · Lever 7-8)

> 本文件是**自包含、可直接由 subagent 执行**的实施规范。subagent **不需要回看主对话**。
> 只做视觉/前端层。改文件:`web/css/app.css`、`web/js/app.js`、`web/js/icons.js`、`web/js/pages/setup.js`。
> **禁止碰** `storage/`、`gateway/`、`providers/`、`accounts/`、后端路由、data/sqlite。
> 红线:路由 slug、导航 key/文案、表单字段名/顺序、后端 API、中文里**零 em-dash (——)**。

---

## 0. 总目标

两个 Lever,每个一颗 commit,做完 push:

- **Lever 7**:Rail 侧栏收缩/展开(展开显示功能全称,收起纯图标,状态记忆)
- **Lever 8**:接入指南页重构(分块卡片布局 + 消灭内联 style 与幽灵变量)

每 Lever 结束:跑 `pytest -q tests/test_web_assets.py`(ESM 校验)→ commit(§10 message)→ push → 更新 `redesign-audit/05-implementation-log.md`(§11)。

**不做完 Lever 7 不进 Lever 8。** 只 `git add` 明确文件,绝不用 `git add -A`(工作树有 `config.toml`、`data/backup/credentials.key.latest` 已 ignore,不会出现;但 `redesign-audit/06-rail-collapse-plan.md`、`07-frontend-polish-spec.md` 是 untracked 文档,不要 add 进功能 commit)。

---

## 1. LEVER 7 · Rail 侧栏收缩/展开

### 1.1 现状(先读确认)

- `web/js/app.js`:`<aside class="rail">` 内 `.rail-brand`(logo)+ `.railnav`(9 个 `.rail-item`,只 `v-html="n.i"` 图标,`:title="n.l"` 悬停提示)+ `.rail-foot`(主题切换 `.rail-icon`)。
- `web/css/app.css` 134-161 行:`.shell{grid-template-columns:56px minmax(0,1fr)}`、`.rail` 56px 竖排、`.rail-item` 40x40 纯图标、`.rail-item.on::before` 左侧橙竖条。
- 移动端 `<760px`(app.css 706-713 行):rail 沉底为底部 tab 条,`.rail-brand,.rail-foot{display:none}`。

### 1.2 交互设计

- **默认收起**(56px 纯图标),与现状一致。
- **展开态**:rail 宽 56px → **200px**,每个 `.rail-item` 变 `[icon] + 功能名` 横排。
- **切换入口**:rail 底部新增**收缩/展开按钮**(双箭头 icon)。
- **状态记忆**:`localStorage` 存 `cb_gw_rail`(`'expanded'`/`'collapsed'`),刷新保持。
- **移动端**:`<760px` 下展开态**不生效**(rail 已沉底 tab 条)。

### 1.3 `web/js/icons.js` 新增两个 icon(stroke-width=1.8,与现有一致)

```js
chevronL:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><polyline points="15 18 9 12 15 6"/></svg>',
chevronR:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><polyline points="9 18 15 12 9 6"/></svg>',
```

### 1.4 `web/js/app.js`

1. `setup()` 内新增:
   ```js
   const railOpen=ref(localStorage.getItem('cb_gw_rail')==='expanded');
   function toggleRail(){railOpen.value=!railOpen.value;try{localStorage.setItem('cb_gw_rail',railOpen.value?'expanded':'collapsed')}catch(_){}}
   ```
2. `return{...}` 加 `railOpen,toggleRail`。
3. template:
   - `<aside class="rail" :class="{open:railOpen}">`
   - `.rail-item` 改为:
     ```html
     <div v-for="n in nav" :key="n.k" class="rail-item" :class="{on:page===n.k}" @click="go(n.k)" :title="n.l">
       <span class="rail-ic" v-html="n.i"></span><span class="rail-lbl" v-if="railOpen">{{n.l}}</span>
     </div>
     ```
   - `.rail-foot` 底部、主题按钮**上方**新增:
     ```html
     <button class="rail-icon" @click="toggleRail" :title="railOpen?'收起侧栏':'展开侧栏'" v-html="railOpen?I.chevronL:I.chevronR"></button>
     ```

### 1.5 `web/css/app.css`

1. `.shell` 列宽改 CSS 变量:
   ```css
   .shell{display:grid;grid-template-columns:var(--rail-w,56px) minmax(0,1fr);min-height:100vh;background:var(--bg);color:var(--fg)}
   ```
2. `.rail` 加过渡 + 展开态:
   ```css
   .rail{...现有...;transition:width var(--dur-base) var(--ease)}
   .rail.open{width:200px;align-items:stretch;padding:10px 8px}
   .rail.open .rail-brand{width:100%;justify-content:flex-start;padding:0 10px;gap:8px}
   .rail.open .railnav{align-items:stretch}
   .rail.open .rail-item{width:100%;justify-content:flex-start;padding:0 10px;gap:10px}
   .rail.open .rail-item.on::before{left:-8px}
   .rail-lbl{display:none}
   .rail.open .rail-lbl{display:inline;font-size:var(--fs-b);font-weight:600;white-space:nowrap;color:inherit}
   ```
   - `.rail-item` 现有 `display:grid;place-items:center` 需改为 `display:flex;align-items:center;justify-content:center`(收起时仍居中,展开时靠左)。注意收起态 `.rail-item` 保持 40x40 居中,展开态变 `width:100%` 靠左。
   - `.rail-ic` 加 `display:inline-flex;flex:0 0 auto`。
3. 移动端 `<760px` 追加(确保展开态不破坏底部 tab 条):
   ```css
   .rail.open{width:100%}
   .rail.open .rail-lbl{display:none}
   ```
   (沉底 tab 条下 `.rail` 已是 `flex-direction:row;justify-content:space-around`,展开态只影响宽度,label 隐藏即可。)

### 1.6 Lever 7 完成判据

- 默认收起 56px 纯图标;点底部双箭头展开 200px 显示全称;再点收起。
- 刷新后状态保持(localStorage `cb_gw_rail`)。
- 移动端(≤760px)沉底 tab 条,展开态不显示文字。
- `pytest -q tests/test_web_assets.py` 通过。

---

## 2. LEVER 8 · 接入指南页重构

### 2.1 现状问题(先读 `web/js/pages/setup.js` 确认)

- 一个超长 `.card` 里塞了 4 块:接入信息 / 客户端向导(tabs)/ Codex 一键配置 / 快速验证,层级不清。
- **大量内联 style**(`style="margin-bottom:12px"`、`style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:12px"`、`style="color:var(--fg2)"` 等)。
- **幽灵变量残留**(app.css 中不存在,渲染无效):`var(--blue-soft)`、`var(--blue-border)`、`var(--fg2)`、`var(--green)`、`var(--red)`、`var(--blue)`。正确变量是 `--fg-2`/`--fg-3`/`--ok`/`--err`/`--accent` 等。

### 2.2 重构目标(纯前端,零功能/文案改动)

把 setup 页拆成**4 个独立 `.card`**,每块视觉层级清晰,消灭全部内联 style 和幽灵变量,改用 app.css 的 token 类。

**保留不动**:所有 `presets`、`envBlock()`、`curlBlock()`、`codexSetup()`、`copy()`、`load()` 等逻辑与函数;所有用户可见文案;所有后端调用(`/admin/settings`、`/admin/api-keys`、`/admin/codex/*`)。

### 2.3 结构(4 卡)

```
Card 1 · 接入信息 (现有 .card 原样,已是 info-list,基本保留)
Card 2 · 客户端接入向导 (setup-tabs + setup-panel,保留 tabs 逻辑)
Card 3 · Codex 一键配置 (独立成卡,含专用处理说明 + 一键写入 + 状态徽章 + 结果)
Card 4 · 快速验证 (独立成卡,curl 块)
```

### 2.4 具体改动

1. **拆卡**:把 template 里"客户端向导"和"Codex 一键配置"从同一个 `.card` 拆成两个独立 `.card`(各自 `.card-h` + `.card-p`)。"快速验证"已是独立 `.card`,保留。
2. **消灭内联 style**:
   - `style="margin-bottom:12px"` → 用 `.mb-3`(已有工具类,app.css 588-590 行)。
   - `style="margin-top:16px"` → `.mt-4`。
   - `style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:12px"` → 新增一个 class `.codex-grid{display:grid;grid-template-columns:1fr 1fr;gap:var(--sp-3);font-size:var(--fs-s)}`(加到 app.css),template 用它。
   - `style="color:var(--fg2)"` → `class="muted"`(已有,app.css 220 行)。
   - `style="color:var(--green)"` → `class="text-ok"`(需在 app.css 加 `.text-ok{color:var(--ok)}`,或复用 `--ok`;检查是否已有,没有就加)。
   - `style="color:var(--red)"` → `class="text-err"`(已有,app.css 591 行)。
   - `style="color:var(--blue)"` → `class="text-accent"`(需加 `.text-accent{color:var(--accent)}`,或复用现有 accent 类)。
   - `style="background:var(--blue-soft);border-color:var(--blue-border)"` → 用 `.callout`(已有,app.css 543 行)或新增 `.callout.accent{background:var(--accent-soft);border-color:var(--accent-border)}`。
   - `style="border:1px solid var(--blue-border);border-radius:var(--r);padding:16px;background:var(--blue-soft)"` → 用 `.card-p` 或 `.callout` 替代。
   - `style="flex:1;min-width:280px;margin:0"` → 新增 `.field-grow{flex:1;min-width:280px;margin:0}` 或复用现有。
   - `style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:var(--r);font-size:13px;font-family:var(--mono);background:var(--bg)"` → 用 `.field input` 现有样式(已是全宽 + mono 可选),去掉内联。
   - `style="white-space:nowrap"` → `.btn` 已有 `white-space:nowrap`,去掉。
   - `style="margin-top:12px"` → `.mt-3`。
   - `style="margin-top:4px"` → `.mt-1`。
   - `style="margin-top:6px;font-size:12px;color:var(--fg2)"` → `.hint`(已有,app.css 360 行)。
   - `style="margin-top:8px;font-size:12px;color:var(--blue)"` → `.hint text-accent`。
   - `style="margin-bottom:8px"` → `.mb-2`。
   - `style="margin-bottom:10px"` → `.mb-3`。
   - `style="margin-bottom:16px"` → `.mb-4`。
   - `style="font-weight:600;font-size:13px;margin-bottom:10px"` → `.card-h` 或 `.text-bold mb-3`。
   - `style="font-weight:600;font-size:14px;margin-bottom:4px"` → `.text-bold mb-1` + 适当字号类。
   - `style="font-weight:600;margin-bottom:8px"` → `.text-bold mb-2`。
   - `style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;margin-bottom:12px"` → 新增 `.codex-row{display:flex;gap:var(--sp-3);align-items:flex-end;flex-wrap:wrap;margin-bottom:var(--sp-3)}`。
   - `style="display:flex;gap:8px;flex-wrap:wrap"` → 已有 `.status-line`(app.css 219 行)或 `.row`(585 行),用 `.status-line`。
   - `style="padding:12px;border-radius:var(--r);background:var(--bg);border:1px solid var(--border)"` → 用 `.testbox`(已有,app.css 516 行)或 `.callout`。
   - `style="display:flex;gap:12px;align-items:baseline;min-width:0"` 等其余 → 逐一用现有工具类替换。
3. **Codex 状态徽章**:`<span class="badge" :class="codexStatus.config_has_buddy2api?'ok':'inactive'">` 等已用 badge,保留。
4. **Codex 结果块**:`style="color:var(--green)"` → `class="text-ok"`;`style="color:var(--red)"` → `class="text-err"`。
5. **app.css 新增工具类**(集中放一处,如 utilities 区):
   ```css
   .text-ok{color:var(--ok)}
   .text-accent{color:var(--accent)}
   .callout.accent{background:var(--accent-soft);border-color:var(--accent-border)}
   .codex-grid{display:grid;grid-template-columns:1fr 1fr;gap:var(--sp-3);font-size:var(--fs-s)}
   .codex-row{display:flex;gap:var(--sp-3);align-items:flex-end;flex-wrap:wrap;margin-bottom:var(--sp-3)}
   .field-grow{flex:1;min-width:280px;margin:0}
   ```
   (若这些类名与现有冲突,改用更具体前缀如 `.setup-*`。)

### 2.5 Lever 8 完成判据

- setup 页 4 块独立卡片,层级清晰。
- `git grep -n "var(--blue\|var(--green\|var(--red\|var(--fg2\|style=" web/js/pages/setup.js` 应**无残留**(或仅剩极少数无法避免的)。
- 功能/文案零改动;`pytest -q tests/test_web_assets.py` 通过。

---

## 3. 交付后自检 (Hard Pre-Flight)

- [ ] `git grep -n "—" -- web/` → 0(零 em-dash)
- [ ] `git grep -n "var(--blue\|var(--green\|var(--red\|var(--fg2" -- web/` → 0
- [ ] 导航 key/文案未变;后端调用未变
- [ ] rail 默认收起、展开显示全称、刷新记忆、移动端忽略
- [ ] setup 页无内联 style 残留、无幽灵变量

---

## 4. 验证命令

```bash
pytest -q tests/test_web_assets.py   # ESM 语法校验,每 Lever 后跑
pytest -q                            # 完整冒烟(12 个后端失败为 pre-existing,与本任务无关,忽略)
```

---

## 5. Commit 纪律

```
Lever 7:  feat(web): collapsible rail sidebar with expand/collapse toggle
Lever 8:  feat(web): restructure setup guide into sectioned cards, drop inline styles
```

每个 commit 前:`git add web/js/app.js web/js/icons.js web/css/app.css`(Lever 7)、`git add web/js/pages/setup.js web/css/app.css`(Lever 8);`git commit -m "..."`;`git push origin refactor/web-console-ia`。

**不要 add**:`redesign-audit/06-rail-collapse-plan.md`、`redesign-audit/07-frontend-polish-spec.md`(untracked 文档,不进功能 commit)。

---

## 6. 实施日志 `redesign-audit/05-implementation-log.md`(每 Lever 追加)

```markdown
## Lever 7 — <name>
- 状态: ✔ done
- 改动文件: web/js/app.js, web/js/icons.js, web/css/app.css
- commit: <hash8> <msg>
- 说明: <2-3 行>
```

---

## 7. 兜底

- 若某 Lever 因结构假设不符做不下去:停下,不要硬改坏功能,把差异写进日志并报告"卡住"+原因。
- 若 setup 页某处内联 style 无法用现有类表达,新增最小工具类(见 §2.4.5),不要用 `!important` 硬覆盖。
- 中文文案零 em-dash;英文 commit message 可含 em-dash 但**中文 UI 零**。
