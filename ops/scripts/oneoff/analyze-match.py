"""Pass 3 (decisive): do official sessions match gateway traesolo requests in time+size?
If yes -> usage_type=7 includes SOLO gateway traffic (billing real).
If no  -> those sessions are separate app-side usage; gateway traffic likely unbilled.
"""
import json, time
from datetime import datetime
from collections import defaultdict

D = json.load(open("analyze-data.json", encoding="utf-8"))

def norm(name):
    n = (name or "").strip().lower().replace(" ", "")
    return n.replace("官方�?, "").replace("official", "")

sess = []
for s in D["sessions"]:
    ei = s.get("extra_info") or {}
    sess.append({"m": norm(s.get("model_name")), "in_full": int(ei.get("input_token") or 0),
                 "out": int(ei.get("output_token") or 0), "t": int(s.get("usage_time") or 0),
                 "c": float(s.get("credits_float") or 0), "sid": (s.get("session_id") or "")[:8]})

logs = D["solo_logs"]
matched, unmatched = 0, 0
print("=== official session vs gateway request (±90s, same model) ===")
for s in sorted(sess, key=lambda x: x["t"]):
    if not s["m"]:
        continue
    cands = [l for l in logs if norm(l["model"] or "") == s["m"] and abs(l["created_at"] - s["t"]) <= 90]
    if cands:
        best = min(cands, key=lambda l: abs(l["created_at"] - s["t"]))
        dt = best["created_at"] - s["t"]
        pr = best["prompt_tokens"] or 0
        ratio = (pr / s["in_full"]) if s["in_full"] else 0
        flag = "TIME-SIZE MATCH" if 0.7 <= ratio <= 1.4 else "time-only"
        matched += 1
        print(f"  {datetime.fromtimestamp(s['t']):%m-%d %H:%M:%S} {s['m']:<18} off_in={s['in_full']:<7} "
              f"gw_prompt={pr:<7} ratio={ratio:.2f} dt={dt:+3d}s credits={s['c']:.3f} [{flag}]")
    else:
        unmatched += 1
        print(f"  {datetime.fromtimestamp(s['t']):%m-%d %H:%M:%S} {s['m']:<18} off_in={s['in_full']:<7} "
              f"credits={s['c']:.3f}  -- no gateway request within 90s")
print(f"\nmatched(time window)={matched} unmatched={unmatched}")

# gateway GLM-5.3 on 8-31 14:40-15:10 window detail
print("\n=== gateway GLM-5.3 requests on 8-31 14:40-15:10 ===")
w = [l for l in logs if norm(l["model"] or "") == "glm-5.3"
     and "2026-08-31 14:40" <= datetime.fromtimestamp(l["created_at"]).strftime("%Y-%m-%d %H:%M") <= "2026-08-31 15:10"]
for l in sorted(w, key=lambda x: x["created_at"]):
    print(f"  {datetime.fromtimestamp(l['created_at']):%H:%M:%S} prompt={l['prompt_tokens']:<8} "
          f"completion={l['completion_tokens']:<6} total={l['total_tokens']}")
print(f"count={len(w)} sum_prompt={sum(l['prompt_tokens'] or 0 for l in w)}")

# where do the gateway's 75 GLM-5.3 reqs on 8-31 actually sit in time?
days = defaultdict(lambda: [0, 0])
hours = defaultdict(int)
for l in logs:
    if norm(l["model"] or "") == "glm-5.3":
        d = datetime.fromtimestamp(l["created_at"])
        days[d.strftime("%m-%d")][0] += 1
        days[d.strftime("%m-%d")][1] += l["total_tokens"] or 0
        hours[d.strftime("%H")] += 1
print("\ngateway GLM-5.3 by day:", {k: tuple(v) for k, v in sorted(days.items())})
print("by hour:", dict(sorted(hours.items())))
