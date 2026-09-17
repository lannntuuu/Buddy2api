# 合入 main · 剩余工作 · 可执行实施规范 (EXECUTION SPEC)

> 本文件是**自包含、可直接由 subagent 执行**的实施规范。subagent **不需要回看主对话**。
> 当前分支:`merge/overhaul-into-main`(从 `origin/main` 创建)。目标:把 Overhaul + host override 改动合入 main。
> 红线:中文零 em-dash;只 `git add` 明确文件,绝不用 `git add -A`。

---

## 0. 背景与已完成部分

用户要把 `refactor/web-console-ia` 分支的视觉 Overhaul + host override 合入 main。main 已重构到 `src/` 结构(web→src/web、providers→src/providers、gateway→src/gateway)。已确认:main 的 src/ 是纯路径移动 + 少量独立改动,与我的改动高度兼容。

**已在 `merge/overhaul-into-main` 分支上完成(工作树状态)**:
- 8 个 `src/web/*` 文件已修改为 Overhaul 版本(app.css、index.html、app.js、icons.js、channels.js、dashboard.js、keys.js、settings.js、setup.js)
- `src/web/fonts/` 已新增(7 个 Geist woff2)
- 14 个 `src/providers/*` 文件已修改(host override 接入)
- `src/providers/host_override.py` 已新增
- `gateway/routers/admin.py` 已 checkout 到**根目录**(A 状态,待移到 src/gateway/routers/)

## 1. 剩余工作(按序执行)

### 1.1 移动 admin.py 到 src/gateway/routers/

当前 `gateway/routers/admin.py` 是 A 状态(在根目录)。main 的对应文件是 `src/gateway/routers/admin.py`。

```bash
# 先确认 src/gateway/routers/admin.py 当前内容(main 版本,无 channel_hosts)
# 把根目录的 admin.py 覆盖到 src/gateway/routers/admin.py
git show "refactor/web-console-ia:gateway/routers/admin.py" > src/gateway/routers/admin.py
# 或:直接把工作树根 gateway/routers/admin.py 复制过去
Copy-Item -Force gateway/routers/admin.py src/gateway/routers/admin.py
# 删除根目录残留
Remove-Item -Recurse -Force gateway
# 取消根目录 admin.py 的 staged 状态
git reset gateway/routers/admin.py
```

> 注意:main 的 admin.py import 是 `from providers.host_override import CHANNEL_HOST_FIELDS`(我的版本),main 的 src/ 内部 import 用 `from providers...`(不带 src 前缀),兼容。**不要改 import 风格**。

### 1.2 清理临时文件

```bash
Remove-Item .tmp_admin_main.py, .tmp_admin_mine.py -Force
```

### 1.3 确认 src/web 的 usage.js/accounts.js/logs.js/quota.js

这些页面我的版本和 main 一致(未出现在 M 状态),**不要动**。只保留已修改的 8 个文件。

### 1.4 验证 src/web 的 index.html 引用的字体路径

我的 web/index.html 引用 `/static/fonts/...`(在 app.css 的 @font-face 里)。确认 main 的静态路由能服务 `src/web/fonts/`。**只需确认路径一致,不要改**。

### 1.5 跑测试

```bash
cd C:\Usr\Code\etc\Buddy2api
python -m pytest -q tests/test_host_override.py tests/test_web_assets.py
python -m pytest -q tests/test_core.py   # 111 passed + 1 pre-existing 忽略
```

> 注意:main 的 tests/ 和我的 tests/ 结构一致。`tests/test_host_override.py` 需要从我的分支复制过来(见 1.6)。

### 1.6 复制 tests/test_host_override.py

main 的 tests/ 没有 `test_host_override.py`(我分支新增)。从我的分支复制:

```bash
git show "refactor/web-console-ia:tests/test_host_override.py" > tests/test_host_override.py
```

### 1.7 提交

```bash
git add src/web/ src/providers/ src/gateway/routers/admin.py tests/test_host_override.py
git commit -m "feat: merge overhaul UI + per-channel host override into src/ structure"
```

> 注意:不要 add `redesign-audit/`(untracked 文档)、`data/backup/credentials.key.latest`、`.tmp_*`。

### 1.8 push

```bash
git push origin merge/overhaul-into-main
```

## 2. 交付后自检

- [ ] `git status --short` 干净(只剩 untracked 的 redesign-audit/ 和 credentials.key.latest)
- [ ] `git grep -n "—" -- src/web/` → 0(中文零 em-dash)
- [ ] `tests/test_host_override.py` 存在且 5 passed
- [ ] `src/gateway/routers/admin.py` 含 `channel_hosts` 校验
- [ ] `src/providers/host_override.py` 存在
- [ ] 无根目录 `gateway/`、`providers/`、`web/` 残留

## 3. 兜底

- 若某文件路径与 spec 不符:按符号名 grep 定位。
- 若 pytest 出现与本次改动无关的新失败:停下报告,不要乱修。
- 若 git 状态混乱:用 `git reset` 取消 staged 后重新 add 明确文件。
- 中文 UI 文案零 em-dash;英文 commit/注释可含。
