# traesolo/deepseek-v4-flash 输出截断 · 调查 spec (INVESTIGATION SPEC)

> 本文件是**自包含、可直接由 subagent 执行**的调查规范。subagent **不需要回看主对话**。
> 目标:**只调查、定位根因、给出修复建议,不直接改代码**。改代码由后续 spec 决定。
> 当前分支:`fix/traesolo-stream-interrupt`(从 origin/main 创建)。
> 红线:中文零 em-dash;只读调查,不改文件。

---

## 0. 现象描述(用户报告)

用户在运行 **traesolo 通道 + deepseek-v4-flash 模型** 时,输出**突然被打断**,最后返回的内容是一段**半截的工具调用标记**:

```
先看 main 的 admin.py 和我的 admin.py 是否有其他差异(除了 channel_hosts)。用 blob 对比。

</parameter>

</invoke>
```

即:模型在生成工具调用(`invoke` 工具)时,输出在**参数闭合标签 `</parameter>` 之后、`</invoke>` 之前**被截断。用户不确定这是:
- (1) **API 层**:traesolo 通道流式响应中途断了,返回不完整
- (2) **Harness 层**:DeepSeek Harness 的 agent 输出被截断(类似之前 workflow 3 连败)

## 1. 调查目标

确定截断发生在哪一层,以及 Buddy2api 的 traesolo 通道是否存在**可修复的健壮性缺陷**。

## 2. 调查步骤(按序,只读)

### 2.1 理解 traesolo 流式链路

通读 `src/providers/traesolo/chat.py` 的流式处理:
- `_make_client()`(约 59-65 行):流式不设总超时/读超时,仅首字节 10s
- SSE 解析(metadata/output/extra_info/token_usage/done/error 事件)
- `_merge_tool_call_delta`(326-355 行)、`_merge_tool_calls`(358-380 行)、`_clean_tool_calls`(383-405 行):工具调用片段合并
- 流式生成器(约 419-520 行):tool_calls 合并、usage、done 处理

**重点问题**:
- 上游 SOLO 流中断(连接断开/EOF/error 事件)时,代码如何处理?是优雅结束还是抛异常?
- 工具调用 arguments 被截断(不完整 JSON)时,是否检测/报错/重试?还是直接透传半截内容?
- 是否有超时/重试机制覆盖"模型生成中途断流"?

### 2.2 检查日志与错误处理

- 搜索 `src/providers/traesolo/` 下的日志、异常、重试逻辑
- 搜索 `ERR_COOLDOWN_S`、`ERR_THRESHOLD`、`MAX_ROTATE` 等重试/冷却机制的使用
- 确认流中断时是否记录日志(便于判断是上游断流还是本地解析问题)

### 2.3 检查上游协议

- `src/providers/traesolo/constants.py`:`AGENT_HOST`、`EP_CHAT`、`FUNCTION`、`DEFAULT_CONFIG`
- 确认 deepseek-v4-flash 这类模型在 traesolo 的 config 里是否有特殊处理(长输出、工具调用)

### 2.4 检查 Harness 侧(如果可能)

- 用户提到"类似之前 workflow 3 连败"——如果截断发生在 Harness agent 层,则**不是 Buddy2api 代码问题**,是执行环境问题
- 判断依据:半截 `</invoke>` 是工具调用 XML 标记,如果 Buddy2api 的 traesolo 通道只是透传 OpenAI SSE,它**不会生成** `</invoke>` 这种标记——这说明截断发生在**上层 agent(DeepSeek Harness)** 而非 Buddy2api

## 3. 输出要求(调查结论,不改代码)

在 `redesign-audit/16-traesolo-interrupt-findings.md` 写调查结论,包含:

1. **截断层判定**:是 API 层还是 Harness 层?给出依据。
   - 关键判据:`</invoke>`/`</parameter>` 是 DeepSeek Harness 的工具调用标记,Buddy2api 的 traesolo 通道只处理 OpenAI SSE,不产生这类标记。若截断内容含这类标记,则截断发生在 Harness 层,与 Buddy2api 无关。
2. **Buddy2api traesolo 通道的健壮性评估**:
   - 流中断时是否优雅处理?
   - 工具调用截断是否检测?
   - 是否有可修复的缺陷?列出具体位置(文件:行号)+ 建议修复方式。
3. **修复建议**(若需要):
   - 具体改哪个文件、哪段逻辑、怎么改
   - 或者明确"无需修复,是 Harness 层问题"

## 4. 纪律

- **只读调查,不改任何文件**(除了写 findings 文档)
- 不 commit、不 push(除非调查结论明确需要,且写在 findings 里说明)
- 中文零 em-dash
- 若发现明确代码 bug,在 findings 里写清楚,但**不要改**

## 5. 兜底

- 若 traesolo 通道代码复杂,聚焦流式生成器 + 工具调用合并 + 错误处理三块
- 若无法确定截断层,给出"最可能"判断 + 依据
