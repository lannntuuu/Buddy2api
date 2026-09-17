# SQLite "database is locked" 频繁出现 — 排查与处理

> 排查日期：2026-09 · 相关代码：`src/storage/repos/_common.py`（`get_conn`）
> 现象：运行期高频报 `sqlite3.OperationalError: database is locked`，栈顶在
> `get_conn()` → `PRAGMA synchronous=NORMAL`（`_common.py` 第 43 行附近），
> 把热鉴权路径（`deps._check_client_auth` 调 `has_api_keys()`）打成 500。

---

## 1. 现场信息

报错栈：

```
_file API keys: has_api_keys() → get_conn()
    _common.py:43: conn.execute("PRAGMA synchronous=NORMAL")
    sqlite3.OperationalError: database is locked
```

- DB 文件：`data/codebuddy_gateway.db`，开发库约 2 MB，生产库约 18 MB。
- `journal_mode=WAL` 已生效（`init_db` 里 `PRAGMA journal_mode=WAL` + `.db-wal/.db-shm`）。
- `get_conn()` 原有 `timeout=5` + `busy_timeout=5000`，却仍在**建连阶段的 PRAGMA** 上抛出。

## 2. 根因

### 2.a WAL 下读不阻塞写，但"写越并发"，连接建立仍会偶发撞锁

WAL 模式读永远不会被写阻塞，所以 `PRAGMA synchronous`（只读）正常不锁库。
但 `get_conn()` 每次请求都新建连接，在高并发短写（额度预留
`reserve_api_key_request` + 请求日志 `record_request`，都用 `BEGIN IMMEDIATE`）下，
会有极小概率在"建连→配置 PRAGMA"这几十毫秒窗口，撞上一条还没结束的写事务 /
`wal_checkpoint` 把持写锁。原有代码对该瞬时锁**不做重试**，直接冒到端点，
于是"经常出现"的一天里协议面上就是偶发性 500。

### 2.b 多 checkout / 多进程共享同一 DB 会把竞争放大

`docs/redesign/04-prod-worktree.md` 里明确：**`CB_GATEWAY_DB_PATH` 必须每个 checkout
独立**，共享则 WAL 锁冲突、数据互写。生产是独立 worktree + 独立 DB，但如果有人
把 dev/prod 的 DB 路径配成同一个，锁竞争会显著加剧。排查时应先确认这一条。

### 2.c 长时间写事务（prune / optimize / backup）为瞬时锁增援

`prune_logs`（每日保留期清理）、`init_db` 里的 `PRAGMA optimize` 都是大范围 DELETE，
跑在同一个 `_lock` 下但**单线程内持锁时间长**；期间其它线程建连容易撞上。

## 3. 处理

### 3.a get_conn() 容错重试（本次已改）

`src/storage/repos/_common.py`：

- 设置 PRAGMA 的这段无锁初始化包进**有界重试**（默认 3 次、退避 50ms/100ms）。
  只对 `"locked"` / `"busy"` 重试，其它 SQLite 错误原样抛出，不吞错。
- busy_timeout 通过新环境变量 `CB_GATEWAY_DB_BUSY_TIMEOUT_MS` 可调（默认仍 5000）。
- 重试耗尽时 `conn.close()` 再抛，不泄漏连接。

效果：瞬时写锁不再穿透到 `has_api_keys()`/热鉴权路径，把"致命 500"降为"偶发等待后成功"。

### 3.b 运维确认项（不改代码）

1. 确认 dev / prod 各自用独立 `CB_GATEWAY_DB_PATH`，不要共享同一 `.db`。
2. 若仍频发，把 `CB_GATEWAY_DB_BUSY_TIMEOUT_MS` 调大到 15000 观察。

## 4. 验证

- 新增 `tests/test_db_get_conn_resilience.py`：持锁线程 + 真 `get_conn()`，
  确认短锁下重试成功、正常路径不回归。
- 存量 `tests/test_core.py`、`tests/test_perf_storage.py` 通过（沙箱相关 2 个用例
  的 `PermissionError` 为环境问题，与本次改动无关）。