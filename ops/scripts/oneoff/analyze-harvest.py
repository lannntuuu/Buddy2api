"""Step 1: harvest all data needed for reverse-engineering TRAE credit billing.

Read-only against official APIs. Writes analyze-data.json in project root.
"""
import sys, json, time, sqlite3
sys.path.insert(0, '.')

OUT = "analyze-data.json"

def harvest_local():
    from storage import database as db
    db.init_db()
    conn = db.get_conn()
    # traesolo requests from logs (token_usage present for this channel)
    rows = conn.execute("""
        SELECT id, model, prompt_tokens, completion_tokens, total_tokens, credit, created_at
        FROM logs WHERE provider='traesolo' AND status_code BETWEEN 200 AND 299
        ORDER BY created_at
    """).fetchall()
    solo_logs = [dict(r) for r in rows]
    tw_logs = conn.execute("""
        SELECT COUNT(*) c, COALESCE(SUM(total_tokens),0) t FROM logs
        WHERE provider='traework'
    """).fetchone()
    conn.close()
    return solo_logs, dict(tw_logs)

async def harvest_official():
    from accounts import auth_manager
    from providers.traesolo import token as ts_token, chat as ts_chat, quota as ts_quota
    from providers.traesolo.constants import UG_HOST
    import httpx

    acct = auth_manager.pick_account(None, provider="traesolo")
    if acct and ts_token.needs_pre_refresh(acct):
        try:
            acct = await ts_token.refresh_account(acct)
        except Exception:
            pass
    if acct is None:
        acct = auth_manager.pick_account(None, provider="traework")
    assert acct, "no trae account"
    tok = str(acct.get("access_token") or "")
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": f"Cloud-IDE-JWT {tok}", "X-User-Region": "CN",
               "User-Agent": ts_token.USER_AGENT}
    client = ts_chat._make_client(30.0)
    out = {}
    try:
        # 1) all usage_type=7 sessions, 90d, paginated
        now = int(time.time()); start = now - 90 * 86400
        sessions, page = [], 1
        while True:
            body = {"start_time": start, "end_time": now, "page_size": 50,
                    "page_num": page, "usage_type": [7]}
            resp = await client.post(f"{UG_HOST}/trae/api/v1/pay/query_user_usage_group_by_session",
                                     headers=headers, json=body)
            data = resp.json()
            batch = data.get("user_usage_group_by_sessions") or []
            sessions.extend(batch)
            if len(batch) < 50 or page > 40:
                break
            page += 1
        out["sessions"] = sessions
        out["sessions_total_declared"] = data.get("total")

        # 2) official model rates
        try:
            details = await ts_chat.fetch_model_details(acct)
        except Exception as exc:
            details = []
            out["rates_error"] = str(exc)[:200]
        out["model_rates"] = [
            {"id": d.get("id"), "rate": d.get("rate"), "official": d.get("official")}
            for d in details if d.get("rate") is not None
        ]

        # 3) entitlements + expired
        ent = await ts_quota.fetch_entitlement_list(acct)
        exp = await ts_quota.fetch_expired_ents(acct)
        packs = []
        for p in ((ent.get("data") or {}).get("user_entitlement_pack_list") or []):
            bi = p.get("entitlement_base_info") or {}
            packs.append({
                "entitlement_id": bi.get("entitlement_id"),
                "credits_limit": (bi.get("quota") or {}).get("credits_limit"),
                "start_time": bi.get("start_time"),
                "end_time": bi.get("end_time"),
                "used": (p.get("usage") or {}).get("credits_amount"),
                "product_id": bi.get("product_id"),
            })
        out["packs"] = packs
        out["usage_summary"] = (ent.get("data") or {}).get("usage_summary")
        out["expired"] = (exp.get("data") or {}).get("expired_ent_list") or []
    finally:
        await ts_chat._aclose_client(client)
    return out

def main():
    import asyncio
    solo_logs, tw = harvest_local()
    print(f"local: solo_logs={len(solo_logs)} traework_logs={tw}")
    official = asyncio.run(harvest_official())
    print(f"official: sessions={len(official.get('sessions') or [])} "
          f"rates={len(official.get('model_rates') or [])} packs={len(official.get('packs') or [])}")
    payload = {"harvested_at": int(time.time()), "solo_logs": solo_logs,
               "traework_logs_agg": tw, **official}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"written -> {OUT}")

if __name__ == "__main__":
    main()
