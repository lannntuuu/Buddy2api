"""Historical cache backfill (v2) ‚Ä?robust model matching + sensible default ratio.

The 51 official sessions give us a per-model cache ratio oracle. For models with no
official data on a given day, fall back to model-average; for models with no
official data at all, fall back to a conservative 70% (agent-loop traffic is
typically mostly cache hits).
"""
import sys, time, json, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, '.')
from storage import database as db
db.init_db()
from collections import defaultdict
from providers.traesolo.pricing import trae_credit_from_usage

with open("analyze-data.json", encoding="utf-8") as f:
    D = json.load(f)

def norm(name):
    n = (name or "").strip().lower().replace(" ", "")
    for suf in ("ÂÆòÊñπÁâ?, "Ê≠£ÂºèÁâ?, "official", "official-version", "officialversion"):
        n = n.replace(suf, "")
    return n.strip("-")

DEFAULT_RATIO = 0.70  # agent traffic assumption: 70% of prompt is cache hit

agg: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])  # [cache, full_input]
for s in D["sessions"]:
    ei = s.get("extra_info") or {}
    inp = int(ei.get("input_token") or 0)
    cr = int(ei.get("cache_read_token") or 0)
    if inp <= 0:
        continue
    day = time.strftime("%Y-%m-%d", time.localtime(int(s.get("usage_time") or 0)))
    key = (norm(s.get("model_name") or ""), day)
    agg[key][0] += cr
    agg[key][1] += inp

ratios: dict[tuple, float] = {k: cr / inp for (k, (cr, inp)) in agg.items() if inp > 0}
model_avg: dict[str, float] = {}
model_agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
for (model, _), (cr, inp) in agg.items():
    model_agg[model][0] += cr
    model_agg[model][1] += inp
for m, (cr, inp) in model_agg.items():
    if inp > 0:
        model_avg[m] = cr / inp

# add a few common gateway models (no official data) with the default
KNOWN_GATEWAY_MODELS = {
    "glm-5.3", "glm-5.2", "glm-5.2-flash", "qwen-3.7-plus", "deepseek-v4-flash",
    "deepseek-v4-flash-official", "kimi-k3", "kimi-k2.6", "doubao-seed-2.1-turbo",
}
for m in KNOWN_GATEWAY_MODELS:
    if m not in model_avg:
        model_avg[m] = DEFAULT_RATIO

print("per (model, day) ratios (n>=3 only used):")
N_MIN = 3
ratios_robust = {k: v for (k, _), v in zip(agg.items(), [cr/inp if inp>0 else 0 for (cr,inp) in agg.values()]) if agg[k][1] > 0 and len([1 for s in D['sessions'] if norm(s.get('model_name') or '')==k[0] and time.strftime('%Y-%m-%d', time.localtime(int(s.get('usage_time') or 0)))==k[1]]) >= N_MIN}
for k, v in sorted(ratios_robust.items()):
    print(f"  {k}: {v:.2%}")
print("\nmodel-avg fallback (with default 70% for unknown):")
for m, r in sorted(model_avg.items()):
    print(f"  {m}: {r:.2%}")

conn = db.get_conn()
rows = conn.execute("""
    SELECT id, model, prompt_tokens, completion_tokens, COALESCE(cache_read_tokens,0) AS cr,
           date(created_at,'unixepoch','localtime') AS day
    FROM logs WHERE provider='traesolo'
""").fetchall()

updates = []
applied_per_day = defaultdict(lambda: [0, 0, 0])  # [old_credit, new_credit, n]
for r in rows:
    if r["cr"] > 0 or r["prompt_tokens"] <= 0:
        continue
    mn = norm(r["model"] or "")
    d = r["day"]
    per_day_count = sum(1 for s in D['sessions']
                        if norm(s.get('model_name') or '') == mn
                        and time.strftime('%Y-%m-%d', time.localtime(int(s.get('usage_time') or 0))) == d)
    if per_day_count >= N_MIN and (mn, d) in ratios:
        ratio = ratios[(mn, d)]
    elif mn in model_avg:
        ratio = model_avg[mn]
    else:
        ratio = DEFAULT_RATIO
    est_cache = min(int(round(r["prompt_tokens"] * ratio)), r["prompt_tokens"])
    new_credit = trae_credit_from_usage(
        r["prompt_tokens"], r["completion_tokens"],
        cache_read_tokens=est_cache, cache_creation_tokens=0,
        model=r["model"])
    if new_credit is None:
        continue
    updates.append((est_cache, new_credit, "historical_backfill", r["id"]))
    applied_per_day[d][1] += new_credit
    applied_per_day[d][2] += 1

print(f"\nrows to backfill: {len(updates)}")
with db._lock:
    conn.executemany(
        "UPDATE logs SET cache_read_tokens=?, credit=?, credit_source=? WHERE id=?",
        updates)
    conn.commit()

for a in conn.execute("SELECT id FROM accounts WHERE provider='traesolo'").fetchall():
    s = conn.execute(
        "SELECT COALESCE(SUM(credit),0) FROM logs WHERE account_id=? AND provider='traesolo'",
        (a["id"],)).fetchone()[0]
    conn.execute("UPDATE accounts SET total_credits=? WHERE id=?", (round(float(s), 6), a["id"]))
conn.commit()

tot = conn.execute("SELECT COALESCE(SUM(credit),0) FROM logs WHERE provider='traesolo'").fetchone()[0]
rows_with_cache = conn.execute("SELECT COUNT(*) c FROM logs WHERE provider='traesolo' AND cache_read_tokens>0").fetchone()[0]
print("\nbackfill per-day:")
for d in sorted(applied_per_day):
    _, c, n = applied_per_day[d]
    print(f"  {d}  n={n:<5} new_credit_total={c:>10.2f}")
conn.close()
print(f"\nFINAL traesolo total: {round(tot, 2)} credits")
print(f"rows with cache_read>0: {rows_with_cache}")
