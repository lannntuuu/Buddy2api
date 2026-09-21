"""Aggregations over the logs table: get_stats, get_provider_model_usage."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

from storage.repos._common import get_conn, today_start_ts
from storage.repos.accounts import get_traework_daily_credit
from storage.repos.logs import stream_p95_by_provider


def get_provider_model_usage(filters: Optional[dict] = None) -> dict:
    """Per (provider x model x day) token/credit aggregations from request logs.

    平台层的 `models[*].daily` 是**跨账号合并**后的每日一行（未勾选「按账号分组」
    时前端读的就是这份）；`accounts[*].models[*].daily` 是账号内的每日一行。
    两层的 summary 与 daily 都满足 Σ == 上一层。

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
    has_start = start not in (None, "", "all")
    if has_start:
        where.append("created_at>=?")
        values.append(int(start))
    end = filters.get("end")
    has_end = end not in (None, "", "all")
    if has_end:
        where.append("created_at<=?")
        values.append(int(end))

    sql_where = (" WHERE " + " AND ".join(where)) if where else ""

    # account_count 查询的 WHERE：与主查询同口径，但不含 model 条件
    # （1.2 规则 2：账号层与 model 筛选解耦）。
    count_where = []
    count_values: list[Any] = []
    if provider:
        count_where.append("provider=?")
        count_values.append(provider)
    if has_start:
        count_where.append("created_at>=?")
        count_values.append(int(start))
    if has_end:
        count_where.append("created_at<=?")
        count_values.append(int(end))
    count_sql_where = (" WHERE " + " AND ".join(count_where)) if count_where else ""

    conn = get_conn()
    rows = conn.execute(
        f"""
        SELECT provider,
               model,
               date(created_at, 'unixepoch', 'localtime') AS date,
                account_id,
                account_name,
               COUNT(*) AS requests,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COALESCE(SUM(COALESCE(cache_read_tokens, 0)), 0) AS cache_read_tokens,
               COALESCE(SUM(COALESCE(cache_creation_tokens, 0)), 0) AS cache_creation_tokens,
               COALESCE(SUM(credit), 0) AS credit,
               COALESCE(SUM(duration_ms), 0) AS duration_ms,
               COALESCE(SUM(CASE WHEN first_token_ms IS NOT NULL
                                THEN COALESCE(completion_tokens, 0) END), 0) AS decode_tokens,
               COALESCE(SUM(CASE WHEN first_token_ms IS NOT NULL
                                THEN MAX(0, COALESCE(duration_ms, 0) - first_token_ms) END), 0) AS decode_ms
        FROM logs{sql_where}
        GROUP BY provider, model, date, account_id, account_name
        ORDER BY date DESC, provider ASC, model ASC
        """,
        values,
    ).fetchall()

    # 第二条查询：account_count，只含时间窗(+可选 provider)，不含 model
    # （1.2 规则 2）。conn.close() 必须在这一条之后（1.3）。
    account_count_rows = conn.execute(
        f"""
        SELECT provider, COUNT(DISTINCT COALESCE(account_id, -1)) AS n
        FROM logs{count_sql_where}
        GROUP BY provider
        """,
        count_values,
    ).fetchall()
    account_count_by_provider = {r["provider"]: int(r["n"]) for r in account_count_rows}
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
            "decode_tokens": 0,
            "decode_ms": 0,
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
        target["decode_tokens"] += int(row["decode_tokens"] or 0)
        target["decode_ms"] += int(row["decode_ms"] or 0)

    def _daily_rows(accum: dict) -> list:
        """把 {日期: 原始聚合量} 转成日期降序的日明细行列表。"""
        out = []
        for date_str in sorted(accum, reverse=True):
            f = _finalize(accum[date_str])
            f.pop("duration_ms", None)  # 日明细不暴露裸时长（沿用原有行字段集）
            out.append({"date": date_str, **f})
        return out

    def _finalize(s: dict) -> dict:
        requests = max(1, s["requests"])
        # 与 success_rate 同口径：返回百分数（已 ×100），前端 pct() 直接拼 %
        ratio = (
            (s["cache_read_tokens"] / s["prompt_tokens"] * 100)
            if s["prompt_tokens"] > 0
            else None
        )
        tps = (
            round(s["decode_tokens"] * 1000 / s["decode_ms"], 1)
            if s["decode_ms"] > 0
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
            "tps": tps,
            "cache_hit_ratio": round(ratio, 2) if ratio is not None else None,
        }

    providers_out: dict[str, dict] = {}
    totals = _new_summary()
    for r in rows:
        row = dict(r)
        p = row["provider"] or "workbuddy"
        m = row["model"] or ""
        d = row["date"]
        if p not in providers_out:
            providers_out[p] = {"models": {}, "summary": _new_summary(), "_accounts": {}}
        prov_bucket = providers_out[p]
        # 平台级模型桶：daily 先以日期为键累积**原始聚合量**（构建期是 dict，收尾转成
        # 日期降序的行列表）。SQL 按 (provider, model, date, account) 出行，多账号通道
        # 的同一天会有多行，这里按日期合并 —— 未勾选「按账号分组」时前端直接读这份
        # daily，同一通道同一模型同一天只有一行；派生值（平均耗时 / tps / 命中率）
        # 由合并后的累加量重算，不是对各行再取平均。
        if m not in prov_bucket["models"]:
            prov_bucket["models"][m] = {"daily": {}, "summary": _new_summary()}
        model_bucket = prov_bucket["models"][m]
        model_bucket["daily"].setdefault(d, _new_summary())
        _add(model_bucket["daily"][d], row)
        _add(model_bucket["summary"], row)

        # 账号维度：同一批行多建一层桶；NULL 归一到「未指定账号」。
        aid = row["account_id"]
        akey = str(aid) if aid is not None else "none"
        accounts = prov_bucket["_accounts"]
        if akey not in accounts:
            accounts[akey] = {
                "id": aid,
                "name": f"账号 #{aid}" if aid is not None else "未指定账号",
                "_named": False,
                "models": {},
                "summary": _new_summary(),
            }
        acct = accounts[akey]
        raw_name = str(row["account_name"] or "").strip()
        if raw_name and not acct["_named"]:
            # rows 按日期降序：首个非空名字即最近的账号名
            acct["name"] = raw_name
            acct["_named"] = True
        # 账号内同理：桶按 account_id 归并，同一账号当天改了名也会有多行，按日期合并。
        if m not in acct["models"]:
            acct["models"][m] = {"daily": {}, "summary": _new_summary()}
        acct_model = acct["models"][m]
        acct_model["daily"].setdefault(d, _new_summary())
        _add(acct_model["daily"][d], row)
        _add(acct_model["summary"], row)
        _add(acct["summary"], row)

        _add(prov_bucket["summary"], row)
        _add(totals, row)

    for p, prov_bucket in providers_out.items():
        for model_bucket in prov_bucket["models"].values():
            model_bucket["daily"] = _daily_rows(model_bucket["daily"])
            model_bucket["summary"] = _finalize(model_bucket["summary"])
        prov_bucket["summary"] = _finalize(prov_bucket["summary"])
        accounts = prov_bucket.pop("_accounts", {})
        for acct in accounts.values():
            for model_bucket in acct["models"].values():
                model_bucket["daily"] = _daily_rows(model_bucket["daily"])
                model_bucket["summary"] = _finalize(model_bucket["summary"])
            acct["summary"] = _finalize(acct["summary"])
        account_count = int(account_count_by_provider.get(p) or 0) or len(accounts)
        prov_bucket["account_count"] = account_count
        # §1.4 精确 gate：仅 account_count >= 2（多账号通道）才展开账号层；
        # 单账号通道（含仅 NULL account_id 的行，计为 account_count=1）保持现有三段式，
        # 不输出 accounts 键（§1.1「accounts 仅 account_count >= 2 时出现」/ §0）。
        if account_count >= 2 and accounts:
            ordered = sorted(
                accounts.values(),
                key=lambda a: (
                    -a["summary"]["requests"],
                    a["id"] if a["id"] is not None else 0,
                ),
            )
            prov_bucket["accounts"] = [
                {
                    "id": a["id"],
                    "name": a["name"],
                    "summary": a["summary"],
                    "models": a["models"],
                }
                for a in ordered
            ]

    return {
        "providers": providers_out,
        "totals": _finalize(totals),
    }


def get_stats() -> dict:
    conn = get_conn()
    # 原先对 logs 全表扫 6 次(COUNT/SUM×2/success/error/filtered/avg)合并为一条
    # 条件聚合;today 窗口的 4 条同理。语义逐字段保持:success/error/filtered 的
    # 判定条件与旧 SQL 完全一致,AVG 本身就忽略 NULL。
    overall = conn.execute(
        """
        SELECT COUNT(*) AS total_requests,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COALESCE(SUM(credit), 0) AS total_credit,
               COALESCE(SUM(CASE WHEN status_code BETWEEN 200 AND 299
                                  AND finish_reason NOT IN ('error', 'content_filter')
                            THEN 1 ELSE 0 END), 0) AS success_requests,
               COALESCE(SUM(CASE WHEN status_code < 200 OR status_code >= 300
                                  OR finish_reason = 'error'
                            THEN 1 ELSE 0 END), 0) AS error_requests,
               COALESCE(SUM(CASE WHEN finish_reason = 'content_filter'
                            THEN 1 ELSE 0 END), 0) AS filtered_requests,
               COALESCE(AVG(duration_ms), 0) AS avg_duration_ms
        FROM logs
        """,
    ).fetchone()
    total_requests = overall["total_requests"]
    total_tokens = overall["total_tokens"]
    total_credit = overall["total_credit"]
    success_requests = overall["success_requests"]
    error_requests = overall["error_requests"]
    filtered_requests = overall["filtered_requests"]
    avg_duration_ms = overall["avg_duration_ms"]
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
        SELECT COUNT(*) AS requests,
               COALESCE(SUM(total_tokens), 0) AS tokens,
               COALESCE(SUM(credit), 0) AS credit,
               COALESCE(AVG(duration_ms), 0) AS avg_duration_ms,
               COALESCE(SUM(CASE WHEN status_code BETWEEN 200 AND 299
                                  AND finish_reason NOT IN ('error', 'content_filter')
                            THEN 1 ELSE 0 END), 0) AS success,
               COALESCE(SUM(CASE WHEN status_code < 200 OR status_code >= 300
                                  OR finish_reason = 'error'
                            THEN 1 ELSE 0 END), 0) AS errors,
               COALESCE(SUM(CASE WHEN finish_reason = 'content_filter'
                            THEN 1 ELSE 0 END), 0) AS filtered
        FROM logs WHERE created_at >= ?
        """,
        (today_start,),
    ).fetchone()
    today_success = today["success"]
    today_errors = today["errors"]
    today_filtered = today["filtered"]

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
    seven_days_ago = today_start - 6 * 86400
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
        # 近 7 天流式请求 first_token_ms 的分通道 P95(毫秒);无样本为 {}
        "stream_p95": stream_p95_by_provider(),
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
