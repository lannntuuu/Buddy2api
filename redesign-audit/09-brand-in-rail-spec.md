# 品牌区移入 Rail · 可执行实施规范 (EXECUTION SPEC · Lever 10)

> 本文件是**自包含、可直接由 subagent 执行**的实施规范。subagent **不需要回看主对话**。
> 只改 `web/js/app.js`、`web/css/app.css`。**禁止碰** 其他页面、后端、路由、data。
> 红线:路由 slug、导航 key/文案、表单字段名/顺序、后端 API、中文里**零 em-dash (——)**。

---

## 0. 目标

把品牌区(logo + 项目名 + 版本号)从"rail 顶部只有 logo"和"顶部条 `.shell-head` 里的标题"两处,**统一收进 rail**:
- **rail 收起时**:只显示 logo(现状)。
- **rail 展开时**:显示 `logo + 项目名 + 版本号`,一块出来。
- **顶部条 `.shell-head`**:移除项目名/版本号(避免重复),只保留右侧的 metaTag + 刷新按钮(可考虑左移或留空,见 §2.3)。

## 1. 现状(先读确认)

- `web/js/app.js` 34 行:`<div class="rail-brand" v-html="I.logo"></div>`(rail 顶部,只 logo)
- `web/js/app.js` 47 行:`<div class="shell-title">{{meta.title}}<span class="shell-ver" v-if="meta.version"> v{{meta.version}}</span></div>`(顶部条标题)
- `web/css/app.css` 145-147 行:`.rail-brand`(32x32 logo 块)
- `web/css/app.css` 159-164 行:`.rail.open` 展开态
- `web/css/app.css` 172-178 行:`.shell-head` / `.shell-title` / `.shell-ver`

## 2. 改动

### 2.1 `web/js/app.js`

1. `.rail-brand` 从纯 `v-html="I.logo"` 改为 logo + 文字(展开时显示):
   ```html
   <div class="rail-brand">
     <span class="rail-brand-ic" v-html="I.logo"></span>
     <span class="rail-brand-txt" v-if="railOpen">
       <span class="rail-brand-name">{{meta.title}}</span>
       <span class="rail-brand-ver" v-if="meta.version">v{{meta.version}}</span>
     </span>
   </div>
   ```
2. `.shell-head` 里的 `.shell-title` 移除(品牌已移入 rail),顶部条只留右侧 actions:
   ```html
   <div class="shell-head">
     <div class="shell-actions">
       <span class="tag">{{metaTag}}</span>
       <button class="refresh-cta" @click="hardRefresh"><span v-html="I.refresh"></span><span>刷新</span></button>
     </div>
   </div>
   ```
   - 若 `.shell-head` 只剩右侧内容,可把 `.shell-actions` 的 `justify-content` 从 `flex-end` 改 `flex-start`(左对齐)或保留右对齐,由你判断哪个更协调。**建议左对齐**(顶部条空着,标题没了,右对齐会显得飘)。

### 2.2 `web/css/app.css`

1. `.rail-brand` 改为可容纳文字:
   ```css
   .rail-brand{margin-bottom:10px;color:var(--accent);width:32px;height:32px;
     display:flex;align-items:center;justify-content:center;gap:8px;
     background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--r-m);
     overflow:hidden;transition:width var(--dur-base) var(--ease)}
   .rail-brand-ic{display:inline-flex;flex:0 0 auto}
   .rail-brand-ic svg{width:20px;height:20px}
   .rail-brand-txt{display:none;flex-direction:column;line-height:1.15;min-width:0}
   .rail-brand-name{font-size:var(--fs-m);font-weight:800;color:var(--fg);white-space:nowrap}
   .rail-brand-ver{font-size:var(--fs-xs);color:var(--fg-3);font-family:var(--mono);white-space:nowrap}
   ```
2. `.rail.open .rail-brand` 展开态:
   ```css
   .rail.open .rail-brand{width:100%;justify-content:flex-start;padding:0 10px;height:auto;min-height:40px}
   .rail.open .rail-brand-txt{display:flex}
   ```
3. `.shell-head` 移除标题后,若 `.shell-title`/`.shell-ver` 不再使用,可删或保留(保留无害,但建议删掉避免死代码)。`.shell-actions` 若左对齐:
   ```css
   .shell-actions{display:flex;align-items:center;gap:var(--sp-2);justify-content:flex-start}
   ```

### 2.3 移动端(≤760px)

- 现有移动端 rail 沉底为 tab 条,`.rail-brand,.rail-foot{display:none}`(app.css 708 行)。品牌文字在移动端**不显示**(rail 沉底无空间),保持现状即可,无需额外规则。

## 3. 验证

```bash
pytest -q tests/test_web_assets.py   # ESM 语法校验
```

- 收起:rail 56px,只 logo。
- 展开:rail 200px,logo + 项目名 + 版本号。
- 顶部条不再显示重复标题。
- 移动端(≤760px):rail 沉底 tab 条,无品牌文字。

## 4. 交付后自检

- [ ] `git grep -n "shell-title\|shell-ver" -- web/js/app.js web/css/app.css` → 确认已移除或仅剩注释(无死代码)
- [ ] 文案零改动(meta.title / meta.version 仍是后端返回)
- [ ] `git grep -n "—" -- web/` → 0(零 em-dash)
- [ ] 只 add `web/js/app.js`、`web/css/app.css` + 日志

## 5. Commit 纪律

```
feat(web): move brand title + version into collapsible rail
```

- `git add web/js/app.js web/css/app.css`
- `git commit -m "feat(web): move brand title + version into collapsible rail"`
- `git push origin refactor/web-console-ia`
- 在 `redesign-audit/05-implementation-log.md` 追加 Lever 10 段(格式同前),日志并入功能 commit 或独立 docs commit 均可,保持历史清晰。

## 6. 兜底

- 若 `.shell-head` 移除标题后布局异常(如高度塌陷),保留一个空的 `.shell-head` 容器即可(它承担 sticky 定位),不要删整个容器。
- 若 meta.title 为空(后端未返回),`.rail-brand-name` 为空,不影响布局(white-space:nowrap + 空文本不占位)。
- 中文文案零 em-dash;英文 commit message 可含 em-dash 但中文 UI 零。
