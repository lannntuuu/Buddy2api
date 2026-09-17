#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gateway_log_inspect.py — 只读分析 Buddy2api 网关日志库，复现「11115 上下文超限问题」关键证据。

用法：./venv/Scripts/python.exe 代码检查/gateway_log_inspect.py [-db <prod 库路径>]

默认库：仓库根 data/codebuddy_gateway.db；也可指定 prod 库：
  python 这个脚本.py -db "C:/Usr/Code/etc/Buddy2api-prod/data/codebuddy_gateway.db"

说明：
- 数据库可能被 SQLCipher 列级加密，但 logs / settings 表通常是明文 SQLite，可只读查询。
- 只读：用 file:...?mode=ro 打开，避免改到任何行；若失败会提示用 copy 方式。
- 输出对应诊断报告《11115-context-exceeded-diagnosis.md》的几组关键查询。

复用价值：
- 11115 固定文案签名（glm 的 "100001 > 100000" 与 hy3 的 "input length too long"）
- 模型历史最大成功 prompt_tokens（判断「非固定 100k」）
- 429 限频前导（判断「切换模型诱因」）
"""

import argparse
import datetime
import re
import sqlite3
import sys

DEFAULT_DB = "data/codebuddy_gateway.db"


def _open_readonly(path: str) -> sqlite3.Connection:
    """尝试只读打开；mode=ro 失败则复制到临时再打开（解决锁/写权限问题）。"""
    uri = "file:%s?mode=ro" % str(path).replace("\\", "/")
    try:
        c = sqlite3.connect(uri, uri=True, timeout=5)
        c.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return c, None
    except Exception as e:
        # 复制到仓库 .tmp 再查
        import shutil, tempfile, os
        tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".tmp", "_gw_inspect_copy.db")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        try:
            shutil.copyfile(path, tmp)
            c2 = sqlite3.connect(tmp)
            c2.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            return c2, tmp
        except Exception as e2:
            raise RuntimeError("无法打开库 %s（%r / 复制 %r）" % (path, e, e2))


def section(title: str):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("-db", default=DEFAULT_DB, help="网关 sqlite 库路径")
    ap.add_argument("--limit", type=int, default=25, help="每条查询保留行数")
    args = ap.parse_args()

    con, copied = _open_readonly(args.db)
    cur = con.cursor()
    if copied:
        print("[info] 已复制到临时文件查询:", copied)
    print("[db]", args.db)

    # ---- 1. 11115 错误签名（固定文案 vs 动态计数） ----
    section("1) 11115 上下文超限：模型 / 固定文案 / extError.code / 次数")
    rows = list(cur.execute(
        "SELECT model, error_msg FROM logs WHERE error_msg LIKE '%11115%' "
        "ORDER BY created_at ASC"))
    if not rows:
        print("  （库内无 11115 记录）")
    else:
        agg = {}
        for model, emsg in rows:
            m = re.search(r'"msg":"(.*?)"', emsg)
            msg = m.group(1) if m else "(no msg)"
            tok = re.search(r"(\d+\s*tokens[^\"\\]*)", msg)
            frag = tok.group(1) if tok else msg
            e = re.search(r'"code":"(.*?)"', emsg[emsg.find("extError"):]) if "extError" in emsg else None
            code = e.group(1) if e else "(no extError.code)"
            key = (model, code, frag)
            agg.setdefault(key, 0)
            agg[key] += 1
        for (model, code, frag), n in sorted(agg.items()):
            print(f"  [{n}x] model={model}  extError.code={code}\n         fragment={frag}")

    # ---- 2. 各模型历史最大成功 prompt_tokens（判断「非固定 100k」） ----
    section("2) 模型单次成功最大 prompt_tokens（证明可远超 100k）")
    for r in cur.execute(
        "SELECT model, MAX(prompt_tokens), COUNT(*) FROM logs "
        "WHERE status_code=200 GROUP BY model ORDER BY 2 DESC LIMIT ?",
        (args.limit,)):
        print("  %-22s max=%9d  ok_count=%d" % (r[0], r[1] or 0, r[2]))

    # ---- 3. 429 限频前导（切换模型诱因） ----
    section("3) 近期 429 限频（部分）")
    for r in cur.execute(
        "SELECT model, COUNT(*), datetime(MAX(created_at),'unixepoch','localtime') "
        "FROM logs WHERE status_code=429 GROUP BY model ORDER BY 2 DESC LIMIT ?",
        (args.limit,)):
        print("  %-22s 429_count=%-5d last=%s" % (r[0], r[1], r[2]))

    # ---- 4. workbuddy 上下文限额配置 ----
    section("4) workbuddy 上下文限额配置")
    for r in cur.execute(
        "SELECT key, value FROM settings "
        "WHERE key LIKE 'workbuddy.max_input%' OR key='models'"):
        print("  %s = %s" % (r[0], (r[1] or "")[:300]))

    # ---- 5. 账号分布（是否多账号、11115 落在哪些账号） ----
    section("5) workbuddy 账号 + 各账号 11115 次数")
    for r in cur.execute("SELECT id,name,provider,status FROM accounts WHERE provider='workbuddy'"):
        print("  acct id=%s name=%s status=%s" % (r[0], r[1], r[3]))
    n111 = cur.execute(
        "SELECT account_id,count(*) FROM logs WHERE provider='workbuddy' "
        "AND error_msg LIKE '%11115%' GROUP BY account_id").fetchall()
    print("  11115 按账号分布:", n111 if n111 else "(无)")

    con.close()
    print("\n[done]")


if __name__ == "__main__":
    main()