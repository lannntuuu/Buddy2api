import sqlite3

c = sqlite3.connect("data/codebuddy_gateway.db")
c.row_factory = sqlite3.Row

print("== 蓝图 account: provider x model counts (all time) ==")
for r in c.execute(
    "SELECT provider, model, COUNT(*) AS n FROM logs "
    "WHERE account_name='蓝图' GROUP BY provider, model ORDER BY provider, model"
):
    print(dict(r))

print("== 蓝图 + hy3 family, recent local-time sample (top 10 newest) ==")
for r in c.execute(
    "SELECT id, datetime(created_at,'unixepoch','localtime') AS lt, provider, model, "
    "account_name, finish_reason, status_code, total_tokens, credit "
    "FROM logs WHERE account_name='蓝图' AND model LIKE '%hy3%' "
    "ORDER BY id DESC LIMIT 10"
):
    print(dict(r))

print("== local now (for 14:40 reference) ==")
for r in c.execute("SELECT datetime('now','localtime') AS now_local, strftime('%s','now') AS now_epoch"):
    print(dict(r))
