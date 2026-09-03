# 跑实例 / 开发分离 · prod worktree 操作笔记

## 现状

| 角色 | checkout | 分支 | 端口 | db 路径 | venv |
|---|---|---|---|---|---|
| **dev**（开发代码） | `C:\Usr\Code\etc\Buddy2api` | `main` (= 1b8a6e9 = v2.2.0) | 8787 | `C:\...\data\codebuddy_gateway.db` | `.venv\Scripts\python.exe` |
| **prod**（跑实例） | `C:\Usr\Code\etc\Buddy2api-prod` | `prod`（绑 v2.2.0 tag） | 8788 | `C:\...\data\codebuddy_gateway.db` | `.venv\Scripts\python.exe` |

**两个 checkout 共享同一个 `.git/`**，任何一边 commit 另一边立刻可见。worktree list：

```bash
git worktree list
# C:/Usr/Code/etc/Buddy2api       1b8a6e9 [main]
# C:/Usr/Code/etc/Buddy2api-prod  1b8a6e9 [prod]
```

## 启动 prod server

```powershell
# 在 prod checkout 里
Set-Location C:\Usr\Code\etc\Buddy2api-prod
$env:CB_GATEWAY_DB_PATH = "C:\Usr\Code\etc\Buddy2api-prod\data\codebuddy_gateway.db"
Start-Process -FilePath ".\.venv\Scripts\python.exe" `
  -ArgumentList "-m","gateway.server","--host","127.0.0.1","--port","8788" `
  -WorkingDirectory "C:\Usr\Code\etc\Buddy2api-prod" `
  -RedirectStandardOutput "C:\Usr\Code\etc\Buddy2api-prod\.tmp\prod_server.log" `
  -RedirectStandardError "C:\Usr\Code\etc\Buddy2api-prod\.tmp\prod_server.err" `
  -WindowStyle Hidden
```

admin token 在 stderr 里：`Get-Content .tmp\prod_server.err | Select-String "Admin Token"`。

## 升级 prod（拉上游新版本）

### 场景 A：上游 v2.2.1 / v2.3 出了，直接升
```bash
cd C:/Usr/Code/etc/Buddy2api-prod
git fetch origin --tags
git checkout v2.2.1   # 或 git checkout main（如果要跟 main）
# 装新依赖（如果 requirements 变了）
.\.venv\Scripts\python.exe -m pip install -r ops/requirements/base.txt
# 重启 server
```

### 场景 B：你自己的 dev checkout 上有修复想推到 prod
```bash
# 1) dev 这边
cd C:/Usr/Code/etc/Buddy2api
git checkout -b fix/some-bug
# ... 改代码、commit ...
git log --oneline fix/some-bug ^main
# 2) 在 prod 这边 cherry-pick（或 merge）
cd C:/Usr/Code/etc/Buddy2api-prod
# 不需要 fetch 因为共享 .git，直接 cherry-pick
git cherry-pick <commit-sha>
# 3) 重启 prod server
```

### 场景 C：拉上游 main 上的修复到 prod
```bash
cd C:/Usr/Code/etc/Buddy2api-prod
git fetch origin main
# 看你想 cherry-pick 哪些 commit
git log origin/main --oneline -10
git cherry-pick <upstream-commit-sha>
# 装依赖（如果上游改了 requirements）
.\.venv\Scripts\python.exe -m pip install -r ops/requirements/base.txt
# 重启
```

## 你在 dev 那边想推到 fork 返哺上游

1. 在 GitHub 上 fork `lannntuuu/Buddy2api` 到你自己的账号
2. dev checkout 加远端：
   ```bash
   git remote add fork https://github.com/<your-username>/Buddy2api.git
   ```
3. 推分支：
   ```bash
   git push fork <branch>
   ```
4. 在 GitHub 上开 PR：`<your-username>/Buddy2api` ← `<branch>` → `lannntuuu/Buddy2api:main`

## 关键约束

- **不要在 prod checkout 里 commit 代码改动**。prod 是"使用中"的状态，只通过 `git pull` / `cherry-pick` / 切 tag 来升级。改代码永远在 dev。
- **端口 8787 vs 8788 不要混**。两个 server 同时跑时，dev 用 8787，prod 用 8788。
- **db 路径不要共用**。`CB_GATEWAY_DB_PATH` 必须每个 checkout 独立，否则 WAL 锁冲突、数据互写。
- **admin token 每次重启会变**。生产部署用 `--admin-token <fixed>` 或 `CB_GATEWAY_ADMIN_TOKEN` 环境变量锁死，否则重启就得重新去管理页设置。
- **prod pre-migration 快照**会在每次 server 启动时自动拍到 `C:\Usr\Code\etc\Buddy2api-prod\data\backup\`，跟 dev 的 backup 目录独立。

## 撤销 worktree（不要了就拆）

```bash
# 1. 停 prod server
Stop-Process -Id <pid> -Force

# 2. 切回 dev checkout
cd C:/Usr/Code/etc/Buddy2api
git worktree remove C:/Usr/Code/etc/Buddy2api-prod
git branch -D prod
git tag -d v2.2.0  # 如果想删本地 tag
git push origin :v2.2.0  # 如果想删远端 tag
```
