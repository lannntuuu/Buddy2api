# prod 仓库合并 main · 执行 spec (EXECUTION SPEC)

> 本文件是**自包含、可直接由 subagent 执行**的实施规范。subagent **不需要回看主对话**。
> 目标:把 `origin/main` 的最新代码(Overhaul UI + host override + await fix)合并进本地 prod 仓库的 `prod` 分支。
> 红线:中文零 em-dash;只 `git add` 明确文件。

---

## 0. 背景

- prod 仓库:`C:\Usr\Code\etc\Buddy2api-prod`,当前在 `prod` 分支,远程 `origin` = `git@github.com:lannntuuu/Buddy2api.git`(与主仓库同一远程)。
- prod 分支和 origin/main 是同一历史的两条分叉,内容几乎相同(都是 src/ 重构 + config.toml + start.bat 改动),差异:
  - prod 独有 10 个 commit(内容与 main 的 src/ 重构相同,hash 不同)
  - main 独有 11 个 commit(含 `b6532c0` Overhaul + host override 合入、`d6957e8` await fix)
- 用户已确认:**合并 main 进 prod**(保留两边历史,生成 merge commit)。

## 1. 执行步骤(按序)

### 1.1 确保 origin/main 最新

```bash
cd C:\Usr\Code\etc\Buddy2api-prod
git fetch origin main
```

### 1.2 合并 origin/main 进 prod

```bash
git merge origin/main --no-edit
```

> 预期:只有一个冲突文件 `src/web/js/app.js`(rename/delete + add/add 混合,因 prod 的 src/ 重构把 web/js/app.js 从根 web/ 移入,而 main 的 Overhaul 也改了它)。其他文件自动合并。

### 1.3 解决 app.js 冲突 —— 取 main 版本

冲突解决策略:**采用 main 的版本**(main 的 app.js 是 Overhaul rail 设计,用户已确认用 main 的 Overhaul)。

```bash
# 用 main 的版本覆盖
git checkout --theirs src/web/js/app.js
git add src/web/js/app.js
```

> 若 `--theirs` 不可用(merge 时 ours=prod, theirs=origin/main),则:
> ```bash
> git show "origin/main:src/web/js/app.js" > src/web/js/app.js
> git add src/web/js/app.js
> ```

### 1.4 确认无其他冲突

```bash
git status
# 应只剩 src/web/js/app.js 是 resolved,无其他 unmerged 文件
```

### 1.5 完成合并

```bash
git commit --no-edit
```

### 1.6 验证

```bash
cd C:\Usr\Code\etc\Buddy2api-prod
python -c "import ast; ast.parse(open('src/web/js/app.js',encoding='utf-8').read()); print('js parse ok')" 2>&1
# JS 用 node 语法检查(若有 node)
node --check src/web/js/app.js 2>&1
# 跑测试
python -m pytest -q tests/test_host_override.py tests/test_web_assets.py 2>&1 | Select-Object -Last 3
```

> 若 node 不存在,跳过 node --check,以 pytest 为准。

### 1.7 push

```bash
git push origin prod
```

## 2. 交付后自检

- [ ] `git status --short` 干净(只剩 untracked 的 data/backup/credentials.key.latest,勿动)
- [ ] `git log --oneline -3 prod` 顶部是 merge commit
- [ ] `src/web/js/app.js` 含 Overhaul rail 设计(搜索 `rail` 或 `shell` 关键字)
- [ ] `src/providers/host_override.py` 存在
- [ ] `src/gateway/routers/admin.py` 含 `channel_hosts` 校验
- [ ] 已 push 到 origin/prod

## 3. 兜底

- 若合并出现**额外**冲突文件(超出 app.js):停下,把冲突文件清单报告,不要擅自解决。
- 若 `--theirs` 报错,用 `git show origin/main:src/web/js/app.js` 方式。
- 若 pytest 出现与本次改动无关的新失败:停下报告,不要乱修。
- 中文 UI 文案零 em-dash;英文 commit/注释可含。
