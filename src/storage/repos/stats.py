"""Aggregations over the logs table: get_stats, get_provider_model_usage."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

from storage.repos._common import get_conn, today_start_ts
from storage.repos.accounts import get_traework_daily_credit


def get_provider_model_usage(filters: Optional[dict] = None) -> dict:
    """Per (provider x model x day) token/credit aggregations from request logs.

    filters:
      provider  optional, only that channel
      model     optional, only that model
      start     optional, start unix ts (inclusive)
      end       optional, end unix ts (inclusive)
    """
    filters = filters or {}
    where = []
    values: list[Any] = []

    provider = str(filters.get("provider") or "").strip()
    if provider:
        where.append("provider=?")
        values.append(provider)
    model = str(filters.get("model") or "").strip()
    if model:
        where.append("model=?")
        values.append(model)
    start = filters.get("start")
    if start not in (None, "", "all"):
        where.append("created_at>=?")
        values.append(int(start))
    end = filters.get("end")
    if end not in (None, "", "all"):
        where.append("created_at<=?")
        values.append(int(end))

    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    conn = get_conn()
    rows = conn.execute(
        f"""
        SELECT provider,
               model,
               date(created_at, 'unixepoch', 'localtime') AS date,
               COUNT(*) AS requests,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COALESCE(SUM(COALESCE(cache_read_tokens, 0)), 0) AS cache_read_tokens,
               COALESCE(SUM(COALESCE(cache_creation_tokens, 0)), 0) AS cache_creation_tokens,
               COALESCE(SUM(credit), 0) AS credit,
               COALESCE(SUM(duration_ms), 0) AS duration_ms
        FROM logs{sql_where}
        GROUP BY provider, model, date
        ORDER BY date DESC, provider ASC, model ASC
        """,
        values,
    ).fetchall()
    conn.close()

    def _new_summary() -> dict:
        return {
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "credit": 0.0,
            "duration_ms": 0,
            "cache_hit_ratio": 0.0,
        }

    def _add(target: dict, row: dict) -> None:
        target["requests"] += int(row["requests"] or 0)
        target["prompt_tokens"] += int(row["prompt_tokens"] or 0)
        target["completion_tokens"] += int(row["completion_tokens"] or 0)
        target["total_tokens"] += int(row["total_tokens"] or 0)
        target["cache_read_tokens"] += int(row["cache_read_tokens"] or 0)
        target["cache_creation_tokens"] += int(row["cache_creation_tokens"] or 0)
        target["credit"] += float(row["credit"] or 0)
        target["duration_ms"] += int(row["duration_ms"] or 0)

    def _finalize(s: dict) -> dict:
        requests = max(1, s["requests"])
        # 与 success_rate 同口径：返回百分数（已 ×100），前端 pct() 直接拼 %
        ratio = (
            (s["cache_read_tokens"] / s["prompt_tokens"] * 100)
            if s["prompt_tokens"] > 0
            else None
        )
        return {
            "requests": s["requests"],
            "prompt_tokens": s["prompt_tokens"],
            "completion_tokens": s["completion_tokens"],
            "total_tokens": s["total_tokens"],
            "cache_read_tokens": s["cache_read_tokens"],
            "cache_creation_tokens": s["cache_creation_tokens"],
            "credit": round(s["credit"], 4),
            "duration_ms": s["duration_ms"],
            "avg_duration_ms": int(s["duration_ms"] / requests),
            "cache_hit_ratio": round(ratio, 2) if ratio is not None else None,
        }

    providers_out: dict[str, dict] = {}
    totals = _new_summary()
    for r in rows:
        row = dict(r)
        p = row["provider"] or "workbuddy"
        m = row["model"] or ""
        if p not in providers_out:
            providers_out[p] = {"models": {}, "summary": _new_summary()}
        prov_bucket = providers_out[p]
        if m not in prov_bucket["models"]:
            prov_bucket["models"][m] = {"daily": [], "summary": _new_summary()}
        model_bucket = prov_bucket["models"][m]
        daily_prompt = int(row["prompt_tokens"] or 0)
        daily_cache_read = int(row["cache_read_tokens"] or 0)
        daily_ratio = (
            (daily_cache_read / daily_prompt * 100) if daily_prompt > 0 else None
        )
        model_bucket["daily"].append(
            {
                "date": row["date"],
                "requests": int(row["requests"] or 0),
                "prompt_tokens": daily_prompt,
                "completion_tokens": int(row["completion_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
                "cache_read_tokens": daily_cache_read,
                "cache_creation_tokens": int(row["cache_creation_tokens"] or 0),
                "credit": round(float(row["credit"] or 0), 4),
                "avg_duration_ms": (
                    int(row["duration_ms"] / row["requests"]) if row["requests"] else 0
                ),
                "cache_hit_ratio": (
                    round(daily_ratio, 2) if daily_ratio is not None else None
                ),
            }
        )
        _add(model_bucket["summary"], row)
        _add(prov_bucket["summary"], row)
        _add(totals, row)

    for prov_bucket in providers_out.values():
        for model_bucket in prov_bucket["models"].values():
            model_bucket["summary"] = _finalize(model_bucket["summary"])
        prov_bucket["summary"] = _finalize(prov_bucket["summary"])

    return {
        "providers": providers_out,
        "totals": _finalize(totals),
    }


def get_stats() -> dict:
    conn = get_conn()
    total_requests = conn.execute("SELECT COUNT(*) as c FROM logs").fetchone()["c"]
    total_tokens = conn.execute(
        "SELECT COALESCE(SUM(total_tokens),0) as s FROM logs"
    ).fetchone()["s"]
    total_credit = conn.execute(
        "SELECT COALESCE(SUM(credit),0) as s FROM logs"
    ).fetchone()["s"]
    success_requests = conn.execute(
        """
        SELECT COUNT(*) as c FROM logs
        WHERE status_code BETWEEN 200 AND 299
          AND finish_reason NOT IN ('error', 'content_filter')
        """
    ).fetchone()["c"]
    error_requests = conn.execute(
        "SELECT COUNT(*) as c FROM logs "
        "WHERE status_code < 200 OR status_code >= 300 OR finish_reason='error'"
    ).fetchone()["c"]
    filtered_requests = conn.execute(
        "SELECT COUNT(*) as c FROM logs WHERE finish_reason='content_filter'"
    ).fetchone()["c"]
    avg_duration_ms = conn.execute(
        "SELECT COALESCE(AVG(duration_ms),0) as v FROM logs "
        "WHERE duration_ms IS NOT NULL"
    ).fetchone()["v"]
    active_accounts = conn.execute(
        "SELECT COUNT(*) as c FROM accounts WHERE status='active'"
    ).fetchone()["c"]
    total_accounts = conn.execute(
        "SELECT COUNT(*) as c FROM accounts"
    ).fetchone()["c"]
    active_keys = conn.execute(
        "SELECT COUNT(*) as c FROM api_keys WHERE status='active'"
    ).fetchone()["c"]
    total_keys = conn.execute(
        "SELECT COUNT(*) as c FROM api_keys"
    ).fetchone()["c"]

    today_start = today_start_ts()
    today = conn.execute(
        """
        SELECT COUNT(*) as requests,
               COALESCE(SUM(total_tokens),0) as tokens,
               COALESCE(SUM(credit),0) as credit,
               COALESCE(AVG(duration_ms),0) as avg_duration_ms
        FROM logs WHERE created_at >= ?
        """,
        (today_start,),
    ).fetchone()
    today_success = conn.execute(
        """
        SELECT COUNT(*) as c FROM logs
        WHERE created_at >= ?
          AND status_code BETWEEN 200 AND 299
          AND finish_reason NOT IN ('error', 'content_filter')
        """,
        (today_start,),
    ).fetchone()["c"]
    today_errors = conn.execute(
        """
        SELECT COUNT(*) as c FROM logs
        WHERE created_at >= ? AND (
            status_code < 200 OR status_code >= 300 OR finish_reason='error'
        )
        """,
        (today_start,),
    ).fetchone()["c"]
    today_filtered = conn.execute(
        "SELECT COUNT(*) as c FROM logs "
        "WHERE created_at >= ? AND finish_reason='content_filter'",
        (today_start,),
    ).fetchone()["c"]

    hourly_rows = conn.execute(
        """
        SELECT CAST(strftime('%H', created_at, 'unixepoch', 'localtime') AS INTEGER) as hour,
               COUNT(*) as requests,
               COALESCE(SUM(total_tokens), 0) as tokens,
               COALESCE(SUM(credit), 0) as credit
        FROM logs WHERE created_at >= ?
        GROUP BY hour ORDER BY hour
        """,
        (today_start,),
    ).fetchall()
    hourly_by_hour = {int(r["hour"]): dict(r) for r in hourly_rows}
    hourly = []
    for hour in range(24):
        row = hourly_by_hour.get(hour, {})
        hourly.append(
            {
                "hour": hour,
                "label": f"{hour:02d}:00",
                "requests": int(row.get("requests") or 0),
                "tokens": int(row.get("tokens") or 0),
                "credit": round(float(row.get("credit") or 0), 4),
            }
        )

    # 最近 7 个自然日每日统计
    seven_days_ago = today_start_ts() - 6 * 86400
    daily_rows = conn.execute(
        """
        SELECT date(created_at, 'unixepoch', 'localtime') as date,
               COUNT(*) as requests,
               COALESCE(SUM(total_tokens), 0) as tokens,
               COALESCE(SUM(credit), 0) as credits,
               COALESCE(SUM(COALESCE(cache_read_tokens, 0)), 0) as cache_tokens,
               SUM(CASE WHEN credit_source='live' THEN 1 ELSE 0 END) as n_live,
               SUM(CASE WHEN credit_source='historical_backfill' THEN 1 ELSE 0 END) as n_backfill,
               COUNT(*) as n_total
        FROM logs WHERE created_at >= ?
        GROUP BY date ORDER BY date
        """,
        (seven_days_ago,),
    ).fetchall()
    daily_by_date = {r["date"]: dict(r) for r in daily_rows}
    today_date = date.today()
    daily = []
    for i in range(6, -1, -1):
        day = (today_date - timedelta(days=i)).isoformat()
        daily.append(
            daily_by_date.get(
                day,
                {
                    "date": day,
                    "requests": 0,
                    "tokens": 0,
                    "credits": 0,
                    "cache_tokens": 0,
                    "n_live": 0,
                    "n_backfill": 0,
                    "n_total": 0,
                },
            )
        )

    # Credit 口径说明 + cache status
    try:
        tw_by_day = get_traework_daily_credit(days=30)
    except Exception:
        tw_by_day = {}
    for d in daily:
        tw = tw_by_day.get(d["date"]) or {}
        tw_c = round(float(tw.get("credits") or 0), 4)
        d["traework_credit"] = tw_c
        base = float(d.get("credits") or 0)
        if tw_c > 0 and base <= 0:
            d["credit_source"] = "official"
        elif tw_c > 0:
            d["credit_source"] = "mixed"
        else:
            d["credit_source"] = "pricelist"
        d["credit_is_official"] = tw_c > 0 and base <= 0
        n_live = int(d.get("n_live") or 0)
        n_backfill = int(d.get("n_backfill") or 0)
        n_total = int(d.get("n_total") or 0)
        if n_total == 0 or d.get("requests", 0) == 0:
            d["cache_status"] = "empty"
        elif n_live == n_total:
            d["cache_status"] = "accurate"
        elif n_live > 0:
            d["cache_status"] = "partial"
        elif n_backfill == n_total:
            d["cache_status"] = "approx"
        else:
            d["cache_status"] = "approx"

    model_stats = conn.execute(
        """
        SELECT model, COUNT(*) as count, COALESCE(SUM(total_tokens),0) as tokens,
               COALESCE(SUM(credit),0) as credit,
               COALESCE(AVG(duration_ms),0) as avg_duration_ms
        FROM logs GROUP BY model ORDER BY count DESC LIMIT 10
        """
    ).fetchall()

    key_stats = conn.execute(
        """
        SELECT api_key_name as name, COUNT(*) as count,
               COALESCE(SUM(total_tokens),0) as tokens,
               COALESCE(SUM(credit),0) as credit, MAX(created_at) as last_used_at
        FROM logs
        WHERE api_key_id IS NOT NULL
        GROUP BY api_key_id, api_key_name
        ORDER BY count DESC LIMIT 5
        """
    ).fetchall()

    account_stats = conn.execute(
        """
        SELECT id, name, nickname, status, total_requests, total_tokens,
               total_credits, last_used_at
        FROM accounts
        ORDER BY status='active' DESC, total_requests DESC, id ASC
        LIMIT 5
        """
    ).fetchall()

    recent_logs = conn.execute(
        """
        SELECT id, api_key_name, account_name, model, stream, total_tokens, credit,
               finish_reason, duration_ms, status_code, error_msg, created_at
        FROM logs ORDER BY id DESC LIMIT 8
        """
    ).fetchall()

    conn.close()
    return {
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "total_credit": round(total_credit, 4),
        "success_requests": success_requests,
        "error_requests": error_requests,
        "filtered_requests": filtered_requests,
        "success_rate": round(
            (success_requests / total_requests * 100) if total_requests else 0, 2
        ),
        "avg_duration_ms": int(avg_duration_ms or 0),
        "today": {
            "requests": int(today["requests"] or 0),
            "tokens": int(today["tokens"] or 0),
            "credit": round(float(today["credit"] or 0), 4),
            "success": int(today_success or 0),
            "errors": int(today_errors or 0),
            "filtered": int(today_filtered or 0),
            "success_rate": round(
                (today_success / today["requests"] * 100)
                if today["requests"]
                else 0,
                2,
            ),
            "avg_duration_ms": int(today["avg_duration_ms"] or 0),
            "hourly": hourly,
        },
        "active_accounts": active_accounts,
        "total_accounts": total_accounts,
        "active_keys": active_keys,
        "total_keys": total_keys,
        "daily": daily,
        "model_stats": [dict(r) for r in model_stats],
        "key_stats": [dict(r) for r in key_stats],
        "account_stats": [dict(r) for r in account_stats],
        "recent_logs": [dict(r) for r in recent_logs],
    }
