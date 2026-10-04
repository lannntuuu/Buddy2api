import sqlite3

c = sqlite3.connect("data/codebuddy_gateway.db")
c.row_factory = sqlite3.Row

print("== ACCOUNTS matching 蓝图 ==")
for r in c.execute("SELECT DISTINCT account_name, account_id FROM logs WHERE account_name LIKE '%蓝图%' ORDER BY account_name"):
    print(dict(r))

print("== hy3 model values ==")
for r in c.execute("SELECT DISTINCT model, COUNT(*) AS n FROM logs WHERE model LIKE '%hy3%' GROUP BY model"):
    print(dict(r))

print("== providers ==")
for r in c.execute("SELECT DISTINCT provider, COUNT(*) AS n FROM logs GROUP BY provider"):
    print(dict(r))

print("== overall time range ==")
for r in c.execute("SELECT MIN(created_at) AS mn, MAX(created_at) AS mx, COUNT(*) AS n FROM logs"):
    print(dict(r))
