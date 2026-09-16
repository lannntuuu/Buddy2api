# 幽灵变量清理 · 可执行实施规范 (EXECUTION SPEC · Lever 9)

> 本文件是**自包含、可直接由 subagent 执行**的实施规范。subagent **不需要回看主对话**。
> 只改 `web/js/pages/channels.js`、`web/js/pages/keys.js` 两个文件(仅替换幽灵 token 名)。
> **禁止碰** `storage/`、`gateway/`、`providers/`、`accounts/`、后端路由、data/sqlite、其他页面。
> 红线:路由 slug、导航 key/文案、表单字段名/顺序、后端 API、中文里**零 em-dash (——)**。

---

## 0. 背景

`channels.js`、`keys.js` 的 template 里残留了一批**不存在的 CSS 变量**(app.css 中没有定义),渲染时是无效值(继承或透明)。这是历史债务,与之前 setup 页同源。本次只做**机械替换**:把幽灵变量名映射到现有真实 token,**不改任何文案、逻辑、布局、内联 style 结构**。

## 1. 幽灵变量 → 真实 token 映射表(唯一权威)

| 幽灵变量(不存在) | 替换为(存在) | 语义 |
|---|---|---|
| `var(--red)` | `var(--err)` | 错误红 |
| `var(--green)` | `var(--ok)` | 成功绿 |
| `var(--green-bg)` | `var(--ok-bg)` | 成功浅底 |
| `var(--fg2)` | `var(--fg-2)` | 次级文字 |
| `var(--fg3)` | `var(--fg-3)` | 三级文字(存在,保留) |
| `var(--border2)` | `var(--border-strong)` | 强边框 |
| `var(--blue)` | `var(--accent)` | 品牌橙(原蓝→橙) |
| `var(--blue-bg)` | `var(--accent-soft)` | 品牌浅底 |
| `var(--blue-border)` | `var(--accent-border)` | 品牌边框 |
| `var(--ok-border)` | `var(--ok-border)` | 存在,保留 |
| `var(--ok-fg)` | `var(--ok-fg)` | 存在,保留 |

> 注意:`--fg3`、`--ok-border`、`--ok-fg` 在 app.css **存在**,保留不动。真正要替换的是:`--red`、`--green`、`--green-bg`、`--fg2`、`--border2`、`--blue`、`--blue-bg`、`--blue-border`。

## 2. 改动范围(只这两个文件)

### 2.1 `web/js/pages/channels.js`

按映射表把 template 里所有幽灵变量替换。已知位置(读文件确认后逐一替换):
- `color:var(--red)` → `color:var(--err)`(多处:umErr、chErr、chOf().error)
- `color:var(--fg2)` → `color:var(--fg-2)`(label 多处)
- `color:var(--fg3)` → 保留(存在)
- `border-top:1px solid var(--border2)` → `border-top:1px solid var(--border-strong)`
- `color:var(--green)` → `color:var(--ok)`(官方 ● 标记)

**只替换 token 名,不碰 style 结构、不删内联 style、不改文案。**

### 2.2 `web/js/pages/keys.js`

按映射表替换:
- `var(--blue-bg)` → `var(--accent-soft)`、`var(--blue)` → `var(--accent)`(client_type tag 的 :style 绑定)
- `var(--blue-border)` → `var(--accent-border)`(preset 按钮 :style)
- `var(--green-bg)` → `var(--ok-bg)`(codex callout)
- `var(--ok-border)`、`var(--ok-fg)` → 保留(存在)

**只替换 token 名,不碰 :style 对象结构、不删内联 style、不改文案。**

## 3. 验证

```bash
# 替换后,这两个文件不应再出现幽灵变量(应只剩存在的 token)
git grep -nE "var\(--red|var\(--green|var\(--green-bg|var\(--fg2|var\(--border2|var\(--blue" -- web/js/pages/channels.js web/js/pages/keys.js
# 期望:无输出(exit 1)
```

```bash
pytest -q tests/test_web_assets.py   # ESM 语法校验
```

## 4. 交付后自检

- [ ] `git grep -nE "var\(--red|var\(--green|var\(--green-bg|var\(--fg2|var\(--border2|var\(--blue" -- web/js/pages/channels.js web/js/pages/keys.js` → 0
- [ ] 文案、逻辑、布局零改动(仅 token 名替换)
- [ ] `git grep -n "—" -- web/` → 0(零 em-dash)
- [ ] 只 add 这两个文件 + 日志

## 5. Commit 纪律

```
feat(web): replace ghost CSS vars with real tokens in channels/keys pages
```

- `git add web/js/pages/channels.js web/js/pages/keys.js`
- `git commit -m "feat(web): replace ghost CSS vars with real tokens in channels/keys pages"`
- `git push origin refactor/web-console-ia`
- 在 `redesign-audit/05-implementation-log.md` 追加一段(Lever 9):
  ```markdown
  ## Lever 9 — ghost CSS var cleanup (channels/keys)
  - 状态: ✔ done
  - 改动文件: web/js/pages/channels.js, web/js/pages/keys.js
  - commit: <hash8> <msg>
  - 说明: <2-3 行>
  ```
- 日志文件改动**并入同一颗功能 commit**(不要单独 docs commit;若已提交则用 `git commit --amend` 合并,或追加一颗 docs commit 均可,保持历史清晰即可)。

## 6. 兜底

- 若某处幽灵变量在映射表里没有:停下,报告该 token 名和位置,等主对话给映射,不要自行猜。
- 若替换后某处视觉明显异常(如颜色语义反了):停下报告,不要硬改。
- 中文文案零 em-dash;英文 commit message 可含 em-dash 但中文 UI 零。
