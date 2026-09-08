"""单字符连击退化护栏测试（hy4 系偶发输出跑飞，如整段 "!!!!"）。

约定：
- 流式（ChatStreamObserver）：content 连续同一非空白字符 ≥256 → parser_error，
  现有 parser_error 机制会中止读取并收尾；
- 空白符不重置也不累计（缩进/换行正常）；
- 非流式路径（proxy._collect_stream）同样检测（在 proxy 测试覆盖）。
"""

from __future__ import annotations

import json

from upstream.chat_grammar import ChatStreamObserver, RepeatRunDetector


def _chunk(content: str, finish_reason=None) -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-x",
            "object": "chat.completion.chunk",
            "model": "hy4-preview",
            "choices": [{"index": 0, "delta": {"content": content},
                         "finish_reason": finish_reason}],
        },
        ensure_ascii=False,
    ).encode("utf-8")


def test_detector_triggers_at_limit():
    det = RepeatRunDetector()
    assert det.feed("!" * 255) is False
    assert det.feed("!") is True


def test_detector_whitespace_does_not_reset_or_count():
    det = RepeatRunDetector()
    # 空白不累计：255 个 "!" + 任意空白 + 1 个 "!" 触发（连击跨空白续算）
    assert det.feed("!" * 255) is False
    assert det.feed("\n\n  \n") is False
    assert det.feed("!") is True


def test_detector_char_switch_resets():
    det = RepeatRunDetector()
    assert det.feed("!" * 200) is False
    assert det.feed("?") is False
    assert det.feed("!" * 200) is False  # 重置后未到阈值


def test_detector_multibyte_ok():
    det = RepeatRunDetector()
    assert det.feed("哈" * 300) is True
    det2 = RepeatRunDetector()
    assert det2.feed("正常中文输出" * 50) is False


def test_stream_observer_degenerate_run_aborts():
    obs = ChatStreamObserver("hy4-preview")
    obs.observe_event(_chunk("开头正常句子。\n\n"))
    obs.observe_event(_chunk("!" * 300))
    assert obs.parser_error == (
        "The upstream output degenerated into a repeated character run."
    )
    # 已收的正常内容保留（parser_error 收尾路径不会丢已出流部分）
    assert "".join(obs.content_parts).startswith("开头正常句子。")


def test_stream_observer_normal_output_not_flagged():
    obs = ChatStreamObserver("hy4-preview")
    obs.observe_event(_chunk("正常内容 " * 100))
    obs.observe_event(_chunk("!!!", finish_reason="stop"))
    assert obs.parser_error is None
    assert obs.eof_error() is None


def test_stream_observer_split_run_across_chunks():
    """连击跨 chunk 累计（255+1 分两帧）同样触发。"""
    obs = ChatStreamObserver("hy4-preview")
    obs.observe_event(_chunk("!" * 255))
    assert obs.parser_error is None
    obs.observe_event(_chunk("!"))
    assert obs.parser_error is not None
