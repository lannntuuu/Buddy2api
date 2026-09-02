"""Backfill: recompute traesolo logs.credit with the derived 3-tier formula.

Historical rows have no cache breakdown (cache_read=0 assumption) -> fresh-input price for
the whole prompt; slightly overestimates vs real billing (cache would be cheaper).
accounts.total_credits for provider=traesolo accounts re-summed afterwards.
"""
import sys, sqlite3
sys.path.insert(0, '.')
from providers.traesolo.pricing import trae_credit_from_usage
from storage import database as db

conn = db.get_conn()
rows = conn.execute("""
    SELECT id, model, prompt_tokens, completion_tokens, total_tokens,
           COALESCE(cache_read_tokens,0) AS cache_read_tokens,
           COALESCE(cache_creation_tokens,0) AS cache_creation_tokens
    FROM logs WHERE provider='traesolo' AND credit IS NOT NULL
""").fetchall()
print(f"rows to recompute: {len(rows)}")

updates = []
for r in rows:
    new_credit = trae_credit_from_usage(
        r["prompt_tokens"], r["completion_tokens"],
        cache_read_tokens=r["cache_read_tokens"],
        cache_creation_tokens=r["cache_creation_tokens"],
        model=r["model"])
    if new_credit is None:
        new_credit = 0.0
    updates.append((new_credit, r["id"]))

with db._lock:
    conn.executemany("UPDATE logs SET credit=? WHERE id=?", updates)
    conn.commit()
print("logs.credit updated")

# re-sum per traesolo account
acct_rows = conn.execute("SELECT id FROM accounts WHERE provider='traesolo'").fetchall()
for a in acct_rows:
    s = conn.execute(
        "SELECT COALESCE(SUM(credit),0) FROM logs WHERE account_id=? AND provider='traesolo'",
        (a["id"],)).fetchone()[0]
    conn.execute("UPDATE accounts SET total_credits=? WHERE id=?", (round(float(s), 6), a["id"]))
    print(f"account {a['id']}: total_credits -> {round(float(s),4)}")
conn.commit()

# summary: old vs new
old_sum = conn.execute("SELECT COALESCE(SUM(credit),0) FROM logs WHERE provider='traesolo'").fetchone()[0]
per_day = conn.execute("""
    SELECT date(created_at,'unixepoch','localtime') d, COUNT(*) n, ROUND(SUM(credit),2) c
    FROM logs WHERE provider='traesolo' GROUP BY d ORDER BY d DESC LIMIT 8
""").fetchall()
conn.close()
print(f"\nnew total traesolo credits: {round(old_sum,2)} (v3 with DeepSeek price fixed: was 14977.88; relative model: 17747.20)")
print("NOTE: historical rows still lack cache_read data (all 0) -> DeepSeek input billed at 1.35 fresh price.")
print("      8-31 real list-price is LOWER once cache hits are counted (agent traffic is mostly cache).")
print("recent days:")
for r in per_day:
    print(f"  {r['d']}  n={r['n']:<5} credit={r['c']}")
