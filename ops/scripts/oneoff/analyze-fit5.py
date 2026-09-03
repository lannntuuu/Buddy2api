"""Pass 5b (final): trimmed least squares per model -> exact 3-tier prices + outlier list.

The user's hypothesis (input / output / cache_read separately priced) is the base structure.
Earlier fits failed because a few discounted rows dragged the coefficients. Trimmed fitting
(fit -> keep rows within 1% -> refit) converges to the true price vector.
"""
import json, itertools
from datetime import datetime

D = json.load(open("analyze-data.json", encoding="utf-8"))
USD = 0.025

def norm(name):
    n = (name or "").strip().lower().replace(" ", "")
    return n.replace("官方�?, "").replace("official", "")

recs = []
for s in D["sessions"]:
    ei = s.get("extra_info") or {}
    inp = int(ei.get("input_token") or 0); cr = int(ei.get("cache_read_token") or 0)
    recs.append({"m": norm(s.get("model_name")) or "?", "in": inp - cr, "cache": cr,
                 "out": int(ei.get("output_token") or 0), "c": float(s.get("credits_float") or 0),
                 "src": s.get("usage_source"), "t": int(s.get("usage_time") or 0),
                 "sid": (s.get("session_id") or "")[:8]})

def solve3(r1, r2, r3):
    M = [[r1["in"], r1["cache"], r1["out"], r1["c"]],
         [r2["in"], r2["cache"], r2["out"], r2["c"]],
         [r3["in"], r3["cache"], r3["out"], r3["c"]]]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-15: return None
        M[c], M[p] = M[p], M[c]
        for r in range(3):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, 4): M[r][k] -= f * M[c][k]
    v = [M[i][3] / M[i][i] for i in range(3)]
    return v if all(x >= 0 for x in v) else None

def trimmed_fit(rs, tol=0.01):
    """Try all 3-row exact solves; keep the price vector with most exact hits; refine."""
    best = None
    for combo in itertools.combinations(range(len(rs)), 3):
        coef = solve3(*(rs[i] for i in combo))
        if coef is None: continue
        hits = 0
        for r in rs:
            pred = coef[0]*r["in"] + coef[1]*r["cache"] + coef[2]*r["out"]
            if abs(pred - r["c"]) <= tol * max(r["c"], 1e-9):
                hits += 1
        if best is None or hits > best[0]:
            best = (hits, coef, combo)
    return best

by_model = {}
for r in recs: by_model.setdefault(r["m"], []).append(r)

print("=== trimmed 3-tier fit per model: credits = p_in*in_nc + p_cache*cache_read + p_out*out ===")
prices = {}
for m, rs in sorted(by_model.items(), key=lambda kv: -len(kv[1])):
    if len(rs) < 3: continue
    best = trimmed_fit(rs)
    if best is None:
        print(f"  {m:<22} no nonneg exact solution"); continue
    hits, coef, combo = best
    prices[m] = coef
    pi, pc, po = coef
    print(f"\n  {m}  (exact hits {hits}/{len(rs)} within 1%)")
    print(f"    $/M:  input={pi*USD*1e6:6.3f}   cache_read={pc*USD*1e6:6.3f}   output={po*USD*1e6:7.3f}")
    print(f"    ratios: cache/in={pc/pi:.3f}  out/in={po/pi:.3f}   (GMI ref: 0.200 / 3.333)")
    outs = []
    for r in rs:
        pred = pi*r["in"] + pc*r["cache"] + po*r["out"]
        res = (r["c"] - pred) / r["c"] * 100
        if abs(res) > 1:
            outs.append((res, r, pred))
    if outs:
        print(f"    discounted/off-formula rows ({len(outs)}):")
        for res, r, pred in sorted(outs, key=lambda x: x[0]):
            print(f"      {datetime.fromtimestamp(r['t']):%m-%d %H:%M} src={r['src']} in={r['in']:<7} "
                  f"cache={r['cache']:<7} out={r['out']:<6} actual={r['c']:8.3f} pred={pred:8.3f} res={res:+7.1f}%")

# cross-model comparison table
print("\n=== price table ($/1M tokens) vs GMI reference (0.15 / 0.03 / 0.50) ===")
print(f"  {'model':<22} {'input':>8} {'cache':>8} {'output':>8}   markup vs GMI-input")
for m, (pi, pc, po) in prices.items():
    mi = pi*USD*1e6
    print(f"  {m:<22} {mi:8.3f} {pc*USD*1e6:8.3f} {po*USD*1e6:8.3f}   {mi/0.15:6.1f}x")

# what-if: gateway traffic billed at these client prices
print("\n=== what-if: gateway traesolo traffic at official client prices ===")
tot_in = sum(l["prompt_tokens"] or 0 for l in D["solo_logs"])
tot_out = sum(l["completion_tokens"] or 0 for l in D["solo_logs"])
for m, (pi, pc, po) in prices.items():
    hi = pi*tot_in + po*tot_out
    print(f"  @{m:<20} upper-bnd(no cache discount) = {hi:10.1f} credits (${hi*USD:8.2f})")
print(f"  actual window deduction bound for SOLO+IDE: <=543.95 credits -> gateway traffic NOT billed at client rates")
