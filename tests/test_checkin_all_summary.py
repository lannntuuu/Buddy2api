"""checkin_all 汇总口径回归测试。

背景：POST /admin/accounts/checkin-all 返回的 `credit` 汇总原先只累加
workbuddy 通道，而前端「一键领取结果」弹窗的列表逐行展示全通道（traework /
traesolo 每天 150 积分）的 `row.credit`，表头与列表对不上。本用例钉死
后端口径：`credit` == 全通道已领取行 credit 之和（round 4 位），且不再
返回无人消费的 `credit_deprecated` 字段。

沿用本仓库约定：同步测试函数内用 asyncio.run 驱动协程（未装 pytest-asyncio），
隔离 DB 用 conftest 的 isolated_db fixture。
"""
import asyncio

import providers
from accounts import control_plane


def test_checkin_all_summary_matches_rows(isolated_db, monkeypatch):
    # 消除 checkin 间隔 sleep（checkin_gap_seconds 读该环境变量）
    monkeypatch.setenv("CB_GATEWAY_CHECKIN_GAP_MS", "0")

    # 只跑三个 checkin_supported 通道
    monkeypatch.setattr(
        control_plane.providers,
        "enabled_provider_ids",
        lambda: ["workbuddy", "traework", "traesolo"],
    )

    async def fake_channel_accounts(channel, status="active"):
        if channel == "traesolo":
            return [
                {"id": 31, "name": "solo-a", "nickname": "solo-a"},
                {"id": 32, "name": "solo-b", "nickname": "solo-b"},
            ]
        return [{"id": 10, "name": channel, "nickname": channel}]

    monkeypatch.setattr(control_plane, "_channel_accounts", fake_channel_accounts)

    # workbuddy 无 claim_checkin 属性，走 auth_manager 路径；ok 行还会刷 resources
    async def fake_wb_claim(account):
        return {
            "ok": True,
            "claimed": True,
            "already_claimed": False,
            "credit": 100.0,
            "status_code": 200,
            "message": "领取成功",
            "account_id": 10,
            "account_name": "workbuddy",
        }

    async def fake_resources(account, *args, **kwargs):
        return {}

    monkeypatch.setattr(control_plane.auth_manager, "claim_daily_checkin", fake_wb_claim)
    monkeypatch.setattr(control_plane.auth_manager, "fetch_account_resources", fake_resources)

    # traework / traesolo 走 provider 实例的 claim_checkin（实例属性打桩）
    async def fake_traework_claim(account):
        return {
            "ok": True,
            "claimed": True,
            "already_claimed": False,
            "credit": 150.0,
            "status_code": 200,
            "message": "success",
            "account_id": 10,
            "account_name": "traework",
        }

    async def fake_traesolo_claim(account):
        if int(account.get("id") or 0) == 31:
            return {
                "ok": True,
                "claimed": False,
                "already_claimed": True,
                "credit": 0.0,
                "status_code": 200,
                "message": "今日已领取",
                "account_id": 31,
                "account_name": "solo-a",
            }
        return {
            "ok": False,
            "claimed": False,
            "already_claimed": False,
            "credit": 0.0,
            "status_code": 0,
            "message": "HTTP 500",
            "account_id": 32,
            "account_name": "solo-b",
        }

    monkeypatch.setattr(providers.get_provider("traework"), "claim_checkin", fake_traework_claim)
    monkeypatch.setattr(providers.get_provider("traesolo"), "claim_checkin", fake_traesolo_claim)

    result = asyncio.run(control_plane.checkin_all(None))

    # 汇总口径：credit 必须 == 全通道已领取行 credit 之和（前端「表头 == 列表加起来」）
    assert result["total"] == 4
    assert result["claimed"] == 2
    assert result["already_claimed"] == 1
    assert result["failed"] == 1
    assert result["credit"] == 250.0  # 100(workbuddy) + 150(traework)；旧逻辑只得 100
    row_credit_sum = sum(
        float(r.get("credit") or 0) for r in result["results"] if r.get("claimed")
    )
    assert result["credit"] == round(row_credit_sum, 4)
    assert "credit_deprecated" not in result

    # 抽查：results 各行都带上了 channel 字段
    channels = [r.get("channel") for r in result["results"]]
    assert channels == ["workbuddy", "traework", "traesolo", "traesolo"]
