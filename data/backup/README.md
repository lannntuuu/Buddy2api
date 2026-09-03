# `data/backup/` · gateway db 快照目录

Gateway SQLite 数据库（`codebuddy_gateway.db`）的快照全在这。本目录不受版本控制（`.gitignore` 排除 `data/`），随部署迁移。

## 目录结构

```
data/backup/
├── codebuddy-gateway__manual__20260831-105238.db
├── codebuddy-gateway__pre-dev__20260901-222855.db
├── credentials.key.latest          # 对称密钥，与最近一次快照同步
└── README.md                       # 本文件
```

文件名格式（**严格**）：

```
codebuddy-gateway__<kind>__<YYYYMMDD-HHMMSS>.db
       └─┬─┘  └┬┘ └──────┬──────┘
      固定  类型     本地时间戳
```

- `<kind>` ∈ {`auto`, `manual`, `pre-migration`, `pre-dev`}
- 时间戳是**本地时间**，不是 UTC，方便和日志对照
- 排序按字典序即可得到时间顺序

## 四种 kind 的用途与保留策略

| kind | 触发者 | 保留 | 何时拍 |
|---|---|---|---|
| `manual` | 运维 / 你手动 | **永久**（永不轮转） | 改 schema 前、怀疑出问题时、给客户/同事传一份 |
| `pre-dev` | dev 实验前 | 最近 3 份 | 跑一次性脚本 / 改 `repos/` 前 |
| `pre-migration` | `init_db()` 自动 | 最近 10 份 | 每次 gateway 启动、每次 `init_db()` 被调用 |
| `auto` | 预留 / 调度器 | 最近 5 份 | 未来可加 cron / scheduled task；目前未使用 |

> ⚠️ **生产环境**，`init_db()` 启动时会自动拍 `pre-migration` 一次。
> 测试 fixture 反复 `init_db()` 不会污染，环境变量 `BUDDY2API_BACKUP_ON_INIT=0` 可关闭（CI 已用）。

## 怎么拍一份快照

**手动拍（运维）：**

```bash
# 默认拍 manual，永久保留
python ops/scripts/backup-db.py

# dev 实验前拍一份，3 份后自动轮转
python ops/scripts/backup-db.py pre-dev

# 查看现有快照
python ops/scripts/backup-db.py --list
```

**代码里拍（脚本 / 工具）：**

```python
from storage import backup
backup.snapshot("manual", reason="before schema rewrite")
backup.snapshot("pre-dev", reason="about to mess with backfill")
```

**自动拍：** `init_db()` 已经 hook 了 `pre-migration`。要加 `auto`（24h 一次之类）自己起个调度器，调用 `backup.snapshot("auto", reason="nightly")` 即可。

## 怎么恢复

⚠️ 恢复会覆盖当前 `data/codebuddy_gateway.db`，先确认你已经不需要当前内容了。

```bash
# 1. 停 gateway
#    Windows: ops\stop.bat  /  ctrl-c 那个前台进程
#    Docker:  docker compose down

# 2. 看一下有哪些快照
python ops/scripts/backup-db.py --list

# 3. 挑一个
SNAP=data/backup/codebuddy-gateway__pre-dev__20260901-222855.db

# 4. 覆盖回生产库
#    路径必须是 data/codebuddy_gateway.db（不是 .bak / .old 之类的）
cp "$SNAP" data/codebuddy_gateway.db

# 5. 拷回对称密钥（如果新部署的 data/credentials.key 不存在或对不上）
[ -f data/backup/credentials.key.latest ] && \
  cp data/backup/credentials.key.latest data/codebuddy_gateway.db.credentials.key

# 6. 启动 gateway：Admin Token 会重新生成（除非你显式传 --admin-token）
python -m gateway.server
```

**Windows PowerShell 等价版：**

```powershell
$SNAP = "data\backup\codebuddy-gateway__pre-dev__20260901-222855.db"
Copy-Item $SNAP data\codebuddy_gateway.db -Force
if (Test-Path data\backup\credentials.key.latest) {
  Copy-Item data\backup\credentials.key.latest data\codebuddy_gateway.db.credentials.key -Force
}
```

## credentials.key 是什么 / 为什么必须一起恢复

`codebuddy_gateway.db.credentials.key` 是数据库**列级加密**用的对称密钥（参见 `storage/credential_crypto.py`）。账号表里的 `access_token` / `refresh_token` / `session_state` 字段用这个密钥加密。

**没有这个密钥，备份里的 token 全部解不出来，等于账号失效。** 所以 `backup.snapshot()` 每次都同步拷一份 `credentials.key.latest`。轮转 / 部署时**只拷 .db 不拷 .key 是错的**。

如果你**主动 rotate 了密钥**（手动改文件、跑密钥迁移），那所有用旧密钥加密的 token 都失效，这是设计如此：**别恢复旧密钥**来"挽救"旧 token。

## 与代码的对应关系

| 文件 | 作用 |
|---|---|
| `storage/backup.py` | 唯一的快照实现：`snapshot()` / `list_snapshots()` / 轮转 |
| `storage/database.py` `init_db()` 开头 | 自动 `pre-migration` hook |
| `ops/scripts/backup-db.py` | 手动 CLI |
| 本文件 | 规范 + 恢复指南 |

## 常见问答

**Q: 能不能用 `cp` 直接复制 `codebuddy_gateway.db` 代替？**
A: 能但**不推荐**。Gateway 开着时 `cp` 可能拿到撕裂的 WAL 半页数据；用 `sqlite3.Connection.backup()`（也就是本模块的实现）保证一致性拷贝。关 gateway 之后 `cp` 是 OK 的。

**Q: 备份文件多大？**
A: 通常 < 1 MB（账号 / 设置 / 日志，8/31 全量是 475 KB，9/1 完整 9 个 settings 是 983 KB）。如果哪天超过 100 MB，先看看是不是日志表没清。

**Q: 我想用云盘 / S3 同步 `data/backup/`，可以么？**
A: 可以。`backup.snapshot()` 返回完整路径，丢给 rclone / aws s3 cp 就行。**别**同步 `data/codebuddy_gateway.db`：加密的 token 跟密钥分离，丢一份没意义。

**Q: 命名里能不能带 reason（`pre-dev__backfill_cache_20260901-154743.db`）？**
A: 不能，文件名保持单一可解析格式。reason 写日志或 `reason=` 参数里，grep 日志能反查。
