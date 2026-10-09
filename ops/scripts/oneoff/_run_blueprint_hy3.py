import sqlite3, json

c = sqlite3.connect("data/codebuddy_gateway.db")
c.row_factory = sqlite3.Row

sql = """
SELECT
    id,
    datetime(created_at, 'unixepoch', 'localtime') AS local_time,
    provider,
    model,
    account_name,
    api_key_name,
    stream,
    prompt_tokens,
    completion_tokens,
    total_tokens,
    credit,
    finish_reason,
    status_code,
    duration_ms,
    error_msg,
    reasoning_effort
FROM logs
WHERE account_name = '蓝图'
  AND model LIKE '%hy3%'
  AND provider = 'workbuddy'
  AND datetime(created_at, 'unixepoch', 'localtime') >= (date('now','localtime') || ' 14:40:00')
  AND datetime(created_at, 'unixepoch', 'localtime') <= datetime('now','localtime')
ORDER BY id DESC;
"""

rows = [dict(r) for r in c.execute(sql)]
print(f"ROWS returned: {len(rows)}")
for r in rows[:40]:
    print(r)

print("\n== SUMMARY per model ==")
for r in c.execute("""
SELECT model,
       COUNT(*) AS requests,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(credit), 4) AS total_credit,
       SUM(CASE WHEN status_code >= 400 OR finish_reason='error' THEN 1 ELSE 0 END) AS errors
FROM logs
WHERE account_name = '蓝图'
  AND model LIKE '%hy3%'
  AND provider = 'workbuddy'
  AND datetime(created_at, 'unixepoch', 'localtime') >= (date('now','localtime') || ' 14:40:00')
  AND datetime(created_at, 'unixepoch', 'localtime') <= datetime('now','localtime')
GROUP BY model ORDER BY model
"""):
    print(dict(r))
