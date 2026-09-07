#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Buddy2api 网关 hy4/hy3 可用性探测脚本（只读、标准库实现，无第三方依赖）。

用途：对运行中的网关发送真实请求、记录结构化结果，用于「hy4-preview 日常可用性实测」。
约束（见 redesign-audit/18-hy4-daily-usage-spec.md）：
  - 仅读、不改 db / 不改代码 / 不重启网关；
  - 不硬编码任何密钥：key 必须从 --key 参数或环境变量 HY4_PROBE_KEY 提供；
  - 可重复运行（幂等），结果 JSON 打到 stdout，并追加写入 results jsonl 文件；
  - 请求预算内运行（默认 max_tokens 受限）；

用法：
  python hy4_daily_probe.py --base http://127.0.0.1:8787 --key sk-xxx \
      --model hy4-preview --suite basic
  python hy4_daily_probe.py --base ... --key ... --model hy3-x --suite stream
  # 也可从 db 只读取 key（需要 DB 内 key_secret 可被 gateway 的加解密逻辑还原时）：
  python hy4_daily_probe.py --base ... --model hy4-preview --suite basic \
      --db data/codebuddy_gateway.db --db-key-name probe

说明：
  hy4-preview 是「推理模型」：非流式时 content 可能为 null、主要产出 reasoning_content；
  流式时 content 与 reasoning_content 通过 delta 分片返回。本脚本两种字段都记录。
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置常量（请求预算，避免消耗过多真实配额）
# ---------------------------------------------------------------------------
DEFAULT_MAX_TOKENS = 512       # 普通题上限（spec 要求 <=1024）
LONGCTX_MAX_TOKENS = 512       # 长上下文题单独上限
REQ_TIMEOUT = 180              # 单次请求超时（秒）
RESULTS_FILE = Path(__file__).resolve().parent / "hy4_probe_results.jsonl"


# ---------------------------------------------------------------------------
# 低层 HTTP 工具
# ---------------------------------------------------------------------------
def _post(base, key, path, body, stream=False, timeout=REQ_TIMEOUT):
    """发送 POST 请求，返回 (meta_dict, raw_value)。

    meta 含：http_status, error(可选), ttft_ms, total_ms, chunks
    非流式 raw_value 为解析后的 dict；流式 raw_value 为拼接后的文本片段列表。
    """
    url = base.rstrip("/") + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        },
    )
    t0 = time.time()
    first = None
    chunks = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            if stream:
                # 逐行读取 SSE，记录首片时间与片段数
                accumulated = []
                for raw in resp:
                    line = raw.decode("utf-8", "replace")
                    if not line.strip():
                        continue
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        chunks += 1
                        if first is None:
                            first = time.time()
                        accumulated.append(payload)
                return (
                    {
                        "http_status": status,
                        "ttft_ms": int((first - t0) * 1000) if first else None,
                        "total_ms": int((time.time() - t0) * 1000),
                        "chunks": chunks,
                    },
                    accumulated,
                )
            else:
                text = resp.read().decode("utf-8", "replace")
                return (
                    {
                        "http_status": status,
                        "ttft_ms": int((time.time() - t0) * 1000),
                        "total_ms": int((time.time() - t0) * 1000),
                        "chunks": 1,
                    },
                    text,
                )
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", "replace")
        return (
            {
                "http_status": e.code,
                "error": "HTTPError",
                "ttft_ms": None,
                "total_ms": int((time.time() - t0) * 1000),
                "chunks": 0,
            },
            err_body,
        )
    except Exception as e:  # 网络层其它错误（超时/连接失败等）
        return (
            {
                "http_status": None,
                "error": f"{type(e).__name__}: {e}",
                "ttft_ms": None,
                "total_ms": int((time.time() - t0) * 1000),
                "chunks": 0,
            },
            "",
        )


def _extract_answer(parsed):
    """从非流式响应里抽取答案文本（兼容 content / reasoning_content）。"""
    try:
        msg = parsed["choices"][0]["message"]
        content = msg.get("content") or ""
        reason = msg.get("reasoning_content") or ""
        return content, reason
    except Exception:
        return "", ""


def _extract_stream_answer(accumulated):
    """从流式分片列表里拼接 content 与 reasoning_content，并提取 usage/finish。"""
    content_parts = []
    reason_parts = []
    finish_reason = None
    usage = None
    for chunk_str in accumulated:
        try:
            chunk = json.loads(chunk_str)
        except Exception:
            continue
        if chunk.get("object") != "chat.completion.chunk":
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        if delta.get("content"):
            content_parts.append(delta["content"])
        if delta.get("reasoning_content"):
            reason_parts.append(delta["reasoning_content"])
        fr = choices[0].get("finish_reason")
        if fr:
            finish_reason = fr
        if chunk.get("usage"):
            usage = chunk["usage"]
    return "".join(content_parts), "".join(reason_parts), finish_reason, usage


def _trunc(text, limit=4096):
    """错误体原文截断（spec 要求截断 4k）。"""
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + f"...[truncated {len(text)-limit}]"


# ---------------------------------------------------------------------------
# 用例实现
# ---------------------------------------------------------------------------
def run_basic(base, key, model, log):
    """A 维度：非流式 + 流式各 >=2 例（中/英）。"""
    cases = [
        ("zh_nonstream", False, "用一句话解释什么是闭包（closure）。"),
        ("en_nonstream", False, "Explain what a REST API is in one sentence."),
        ("zh_stream", True, "用一句话告诉我：水在标准大气压下的沸点是多少摄氏度？"),
        ("en_stream", True, "In one sentence, what is the capital of France?"),
    ]
    for name, stream, prompt in cases:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": DEFAULT_MAX_TOKENS,
            "stream": stream,
        }
        meta, raw = _post(base, key, "/v1/chat/completions", body, stream=stream)
        rec = {
            "suite": "basic", "case": name, "model": model, "stream": stream,
            "prompt": prompt, "status": meta.get("http_status"),
            "latency_ms": meta.get("total_ms"), "ttft_ms": meta.get("ttft_ms"),
            "chunks": meta.get("chunks"), "error": meta.get("error"),
        }
        if stream:
            content, reason, fr, usage = _extract_stream_answer(raw or [])
            rec.update({"finish_reason": fr, "usage": usage,
                        "answer_summary": (content or reason)[:200]})
        else:
            try:
                parsed = json.loads(raw) if raw else {}
            except Exception:
                parsed = {}
            content, reason = _extract_answer(parsed)
            rec.update({
                "finish_reason": (parsed.get("choices") or [{}])[0].get("finish_reason"),
                "usage": parsed.get("usage"),
                "answer_summary": (content or reason)[:200],
            })
            if meta.get("error") or meta.get("http_status") not in (200, None):
                rec["error_body"] = _trunc(raw)
        log(rec)
    return None


def run_stream(base, key, model, log):
    """B 维度：流式完整性（含 TTFT 统计、chunk 连续性、finish_reason、usage）。"""
    prompts = [
        ("zh", "请一步步计算 17 * 23，并给出最终答案。"),
        ("en", "Count from 1 to 5 and then say the sum. Step by step."),
    ]
    for tag, prompt in prompts:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": DEFAULT_MAX_TOKENS,
            "stream": True,
        }
        meta, raw = _post(base, key, "/v1/chat/completions", body, stream=True)
        content, reason, fr, usage = _extract_stream_answer(raw or [])
        # chunk 连续性：检查每片都能解析为合法 JSON
        continuity_ok = True
        for c in (raw or []):
            try:
                json.loads(c)
            except Exception:
                continuity_ok = False
                break
        log({
            "suite": "stream", "case": f"stream_{tag}", "model": model, "stream": True,
            "prompt": prompt, "status": meta.get("http_status"),
            "latency_ms": meta.get("total_ms"), "ttft_ms": meta.get("ttft_ms"),
            "chunks": meta.get("chunks"), "chunk_continuity": continuity_ok,
            "finish_reason": fr, "usage": usage,
            "error": meta.get("error"),
            "answer_summary": (content or reason)[:200],
        })
    return None


def run_tools(base, key, model, log):
    """C 维度：单轮 function call + 连续 >=3 轮 tool 往返（含并行工具选择）。"""
    tools = [
        {"type": "function", "function": {
            "name": "get_weather", "description": "查询某城市天气",
            "parameters": {"type": "object", "properties": {
                "city": {"type": "string"}}, "required": ["city"]}}},
        {"type": "function", "function": {
            "name": "calculator", "description": "计算表达式",
            "parameters": {"type": "object", "properties": {
                "expr": {"type": "string"}}, "required": ["expr"]}}},
    ]
    # 1) 单轮：要求调用一个工具
    messages = [{"role": "user", "content": "北京今天天气怎么样？用工具查一下。"}]
    body = {"model": model, "messages": messages, "tools": tools,
            "max_tokens": DEFAULT_MAX_TOKENS, "stream": False}
    meta, raw = _post(base, key, "/v1/chat/completions", body, stream=False)
    try:
        parsed = json.loads(raw) if raw else {}
        msg = (parsed.get("choices") or [{}])[0].get("message", {})
        tool_calls = msg.get("tool_calls") or []
    except Exception:
        parsed, tool_calls = {}, []
    single_ok = bool(tool_calls) and all(tc.get("function", {}).get("arguments") for tc in tool_calls)
    single_args_valid = False
    try:
        if tool_calls:
            json.loads(tool_calls[0]["function"]["arguments"])
            single_args_valid = True
    except Exception:
        single_args_valid = False
    log({
        "suite": "tools", "case": "single_round", "model": model,
        "status": meta.get("http_status"), "latency_ms": meta.get("total_ms"),
        "finish_reason": (parsed.get("choices") or [{}])[0].get("finish_reason"),
        "tool_calls": [tc.get("function", {}).get("name") for tc in tool_calls],
        "tool_args_valid_json": single_args_valid, "tool_called": bool(tool_calls),
        "usage": parsed.get("usage"), "error": meta.get("error"),
        "error_body": _trunc(raw) if (meta.get("error") or meta.get("http_status") not in (200, None)) else None,
    })
    # 2) 连续 3 轮 tool 往返（把工具结果喂回，要求继续调用）
    conv = list(messages)
    if tool_calls:
        conv.append(msg)
        # 模拟工具执行结果
        for tc in tool_calls:
            conv.append({"role": "tool", "tool_call_id": tc["id"],
                         "content": json.dumps({"result": "晴, 25C"})})
        conv.append({"role": "user", "content": "再帮我算一下 123 + 456 等于多少，并用工具。"})
        body2 = {"model": model, "messages": conv, "tools": tools,
                 "max_tokens": DEFAULT_MAX_TOKENS, "stream": False}
        meta2, raw2 = _post(base, key, "/v1/chat/completions", body2, stream=False)
        try:
            p2 = json.loads(raw2) if raw2 else {}
            m2 = (p2.get("choices") or [{}])[0].get("message", {})
            tc2 = m2.get("tool_calls") or []
        except Exception:
            p2, tc2 = {}, []
        # 第三轮：继续追问，验证多轮能力
        if tc2:
            conv.append(m2)
            for tc in tc2:
                conv.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": json.dumps({"result": 579})})
            conv.append({"role": "user", "content": "结合上面两步，给我一句总结。"})
            body3 = {"model": model, "messages": conv, "tools": tools,
                     "max_tokens": DEFAULT_MAX_TOKENS, "stream": False}
            meta3, raw3 = _post(base, key, "/v1/chat/completions", body3, stream=False)
            try:
                p3 = json.loads(raw3) if raw3 else {}
                summary = (p3.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            except Exception:
                p3, summary = {}, ""
            log({
                "suite": "tools", "case": "multi_round_3", "model": model,
                "round2_status": meta2.get("http_status"), "round3_status": meta3.get("http_status"),
                "round2_tool_calls": [tc.get("function", {}).get("name") for tc in tc2],
                "round3_finish": (p3.get("choices") or [{}])[0].get("finish_reason"),
                "summary": summary[:200], "error": meta2.get("error") or meta3.get("error"),
                "error_body": _trunc(raw2) if (meta2.get("http_status") not in (200, None)) else
                              (_trunc(raw3) if (meta3.get("http_status") not in (200, None)) else None),
            })
        else:
            log({"suite": "tools", "case": "multi_round_3", "model": model,
                 "note": "round2 未触发工具调用，跳过 round3", "round2_status": meta2.get("http_status")})
    return None


def run_longctx(base, key, model, log):
    """D 维度：构造 30k-50k 输入（拼接文档+提问），非流式。关注是否触发 11128。"""
    # 用一段中文文本重复拼接逼近 30k 字符（约 10-15k token，安全且够长）
    para = ("这是一个关于人工智能的示例段落。人工智能是计算机科学的一个分支，"
            "致力于创造能够模拟人类智能的系统。") * 1500  # 约 45k 中文字符
    question = "\n\n问题：上面这段文字主要讨论的主题是什么？请用一句话回答。"
    doc = para + question
    body = {
        "model": model,
        "messages": [{"role": "user", "content": doc}],
        "max_tokens": LONGCTX_MAX_TOKENS,
        "stream": False,
    }
    meta, raw = _post(base, key, "/v1/chat/completions", body, stream=False)
    try:
        parsed = json.loads(raw) if raw else {}
        content, reason = _extract_answer(parsed)
    except Exception:
        parsed, content, reason = {}, "", ""
    log({
        "suite": "longctx", "case": "30k_input", "model": model, "stream": False,
        "input_chars": len(doc),
        "status": meta.get("http_status"), "latency_ms": meta.get("total_ms"),
        "finish_reason": (parsed.get("choices") or [{}])[0].get("finish_reason"),
        "usage": parsed.get("usage"),
        "answer_summary": (content or reason)[:200],
        "error": meta.get("error"),
        "error_body": _trunc(raw) if (meta.get("error") or meta.get("http_status") not in (200, None)) else None,
    })
    return None


def run_stability(base, key, model, log):
    """E 维度：同一请求重复 5 次，记录延迟分布。"""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "用一句话说明 2+2 等于几。"}],
        "max_tokens": 256,
        "stream": False,
    }
    latencies = []
    statuses = []
    for i in range(5):
        meta, raw = _post(base, key, "/v1/chat/completions", body, stream=False)
        statuses.append(meta.get("http_status"))
        if meta.get("total_ms") is not None:
            latencies.append(meta.get("total_ms"))
        try:
            parsed = json.loads(raw) if raw else {}
            ok = (parsed.get("choices") or [{}])[0].get("finish_reason") == "stop"
        except Exception:
            ok = False
        log({
            "suite": "stability", "case": f"repeat_{i+1}", "model": model,
            "status": meta.get("http_status"), "latency_ms": meta.get("total_ms"),
            "finish_ok": ok, "usage": (parsed.get("usage") if ok else None),
            "error": meta.get("error"),
        })
    if latencies:
        latencies_sorted = sorted(latencies)
        p50 = latencies_sorted[len(latencies_sorted)//2]
        p95 = latencies_sorted[min(len(latencies_sorted)-1, int(len(latencies_sorted)*0.95))]
    else:
        p50 = p95 = None
    print(f"[stability summary] model={model} statuses={statuses} "
          f"p50={p50}ms p95={p95}ms success={sum(1 for s in statuses if s==200)}/5",
          file=sys.stderr)
    return None


def run_quality(base, key, model, log):
    """G 维度：一道代码题 + 一道中文推理题，人工可读评估（记录摘要）。"""
    code_q = ("用 Python 写一个函数 is_palindrome(s)，判断字符串是否为回文，"
              "忽略大小写与非字母数字字符。给出完整实现。")
    reason_q = ("小明比小红大 3 岁，5 年后两人年龄之和为 39 岁。请问小红今年几岁？请推理。")
    for tag, q in (("code", code_q), ("reasoning_zh", reason_q)):
        body = {
            "model": model,
            "messages": [{"role": "user", "content": q}],
            "max_tokens": DEFAULT_MAX_TOKENS,
            "stream": False,
        }
        meta, raw = _post(base, key, "/v1/chat/completions", body, stream=False)
        try:
            parsed = json.loads(raw) if raw else {}
            content, reason = _extract_answer(parsed)
        except Exception:
            parsed, content, reason = {}, "", ""
        log({
            "suite": "quality", "case": tag, "model": model, "stream": False,
            "status": meta.get("http_status"), "latency_ms": meta.get("total_ms"),
            "finish_reason": (parsed.get("choices") or [{}])[0].get("finish_reason"),
            "usage": parsed.get("usage"),
            "answer_summary": (content or reason)[:400],
            "error": meta.get("error"),
            "error_body": _trunc(raw) if (meta.get("error") or meta.get("http_status") not in (200, None)) else None,
        })
    return None


SUITES = {
    "basic": run_basic,
    "stream": run_stream,
    "tools": run_tools,
    "longctx": run_longctx,
    "stability": run_stability,
    "quality": run_quality,
}


# ---------------------------------------------------------------------------
# 可选：从 db 只读取 key（复用 gateway 的加密逻辑解密 key_secret）
# ---------------------------------------------------------------------------
def resolve_key_from_db(db_path, key_name):
    """只读解析 api_keys 表中某个 key 的明文（不修改 db）。

    仅当运行环境能加载 gateway 的 storage.credential_crypto 模块时可用；
    否则回退为脚本异常，由调用方提供 --key。
    """
    src = Path(__file__).resolve().parent.parent.parent  # 仓库根（tests/manual -> 根）
    sys.path.insert(0, str(src / "src"))
    from storage.credential_crypto import decrypt_secret  # noqa: WPS433
    from pathlib import Path as _P
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT key_secret FROM api_keys WHERE name=? AND status='active'", (key_name,))
    row = cur.fetchone()
    con.close()
    if not row or not row["key_secret"]:
        raise RuntimeError(f"未找到可用的 key: name={key_name}")
    return decrypt_secret(row["key_secret"], _P(db_path))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Buddy2api hy4/hy3 可用性探测（只读）")
    ap.add_argument("--base", default="http://127.0.0.1:8787", help="网关基地址")
    ap.add_argument("--key", default=None, help="API key（或使用环境变量 HY4_PROBE_KEY）")
    ap.add_argument("--model", required=True, help="模型名，如 hy4-preview / hy3-x")
    ap.add_argument("--suite", required=True, choices=list(SUITES.keys()),
                    help="要运行的用例集")
    ap.add_argument("--db", default=None, help="只读解析 key 时使用的 db 路径")
    ap.add_argument("--db-key-name", default="probe", help="db 中用于测试的 key 名称")
    ap.add_argument("--out", default=str(RESULTS_FILE), help="结果 jsonl 追加写入路径")
    args = ap.parse_args()

    key = args.key or os.environ.get("HY4_PROBE_KEY")
    if not key and args.db:
        key = resolve_key_from_db(args.db, args.db_key_name)
    if not key:
        ap.error("必须通过 --key / 环境变量 HY4_PROBE_KEY / --db 提供 API key")

    results = []

    def log(rec):
        rec["ts"] = int(time.time())
        rec["base"] = args.base
        results.append(rec)
        # 实时打印到 stdout（JSON 行）
        print(json.dumps(rec, ensure_ascii=False))
        # 追加写入 jsonl
        with open(args.out, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    SUITES[args.suite](args.base, key, args.model, log)
    print(f"[done] suite={args.suite} model={args.model} cases={len(results)} "
          f"-> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
