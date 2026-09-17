# traesolo/deepseek-v4-flash 输出截断 · 调查结论(FINDINGS)

> 仅调查,不改代码。当前分支:`fix/traesolo-stream-interrupt`。对应 spec:`16-traesolo-interrupt-investigation-spec.md`。

---

## 1. 截断层判定

### 1.1 结论

**截断发生在 Harness 层(DeepSeek Harness agent),不是 Buddy2api traesolo 通道,也几乎不是 SOLO 上游 API 层。**

### 1.2 关键判据(直接证据)

用户截断内容如下(摘自 spec §0):

```
先看 main 的 admin.py 和我的 admin.py 是否有其他差异(除了 channel_hosts)。用 blob 对比。

</parameter>
</invoke>
```

`</parameter>` 与 `</invoke>` 是 **DeepSeek Harness 的 agent 工具调用标记**(XML 风格闭合标签)。检查证据如下:

1. **整个仓库零 XML 工具调用生成代码**。`grep -r "<invoke\|</invoke\|<parameter\|</parameter" src/` 在 Buddy2api 全代码库范围内无任何匹配。最接近的 `parameters` 字段全部是 OpenAI 标准的 JSON 字段名(`tool.function.parameters`,见 `src/providers/traesolo/chat.py:204`、`src/upstream/responses.py:199` 等),与 XML 闭合标签无关。
2. **traesolo 通道只做 OpenAI SSE 透传与 SOLO 事件转换**。`src/providers/traesolo/chat.py:269-296` 的 `parse_solo_line` 把 SOLO `output`/`token_usage`/`done`/`error` 事件转成 OpenAI 风格的 SSE chunk;`src/providers/traesolo/chat.py:463-532` 的 `stream_to_openai` 输出的是 `data: {json}\n\n` 格式。**Buddy2api 不输出任何 XML 闭合标签**。
3. **tool_call 的 arguments 在 Buddy2api 层是纯字符串拼接**(行 352-355),不做 JSON 解析、不打标记包装。闭合标记不可能来自 Buddy2api。

既然 Buddy2api 输出里没有这类标记,而用户截断文本以 `</invoke>` 结尾,字符序列必然来自 **DeepSeek Harness agent 自身在生成响应**:Harness 在收到 Buddy2api 转出的正常 SSE 流后,把模型输出包装成自家 agent 的工具调用协议(即 `<invoke><parameter>...</parameter></invoke>` 之类),**包装与截断都发生在 Harness 层**。

### 1.3 排除 API 层

若截断发生在 SOLO 上游 API 层,用户看到的内容会是 **未闭合的 OpenAI SSE chunk**(残留 `data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"...未闭合..."}}]}}]}` 形态),不会以人类可读的 `</parameter>` 结尾。字符形态完全不匹配。

另外,SOLO 上游若真断流,Buddy2api 会:
- `_run_stream` 中 `try/except httpx.HTTPError`(行 1148-1152)捕获并注入 `event: error` + `data: [DONE]`
- 同时把 `errored=True`,写日志 `status_code=502, error_msg="upstream stream error"`(行 1155-1165,数据落 `db.record_request`)
- 流式 chunk 在 `done` 事件缺位时仍会兜底写 `data: [DONE]`(行 530-532)

用户事后查 Buddy2api 请求日志,若为 API 层断流,会看到 502 + "upstream stream error" 记录。

### 1.4 可能性映射

| 层 | 概率 | 关键依据 |
|---|---|---|
| Harness 层(agent 自身生成/截断) | **高** | XML 闭合标记只可能由 Harness 工具调用协议生成 |
| Buddy2api traesolo 通道 | 极低 | 不输出 XML 标记;流中断会显式注入错误事件 |
| SOLO 上游 API | 极低 | 截断形态不对(应是半截 SSE chunk,而非 XML 闭合) |

> 备注:用户提到"类似之前 workflow 3 连败"——若截断发生在 Harness agent 层,属于执行环境(模型本身产出被 harness 截断、token 用尽、上下文超限、客户端超时等),与 Buddy2api 无关。

---

## 2. Buddy2api traesolo 通道健壮性评估

逐项对照检查清单。

#### 2.1 流中断是否优雅处理

**是,且处理完整。**(`src/providers/traesolo/chat.py:1140-1154`)

- 流内 `httpx.HTTPError`(读超时/连接断):注入 `event: error` + `data: [DONE]`,关闭客户端,记日志。
- 流内无 `done` 事件时(`stream_to_openai` 行 530-532):兜底写 `data: [DONE]`,保证 SSE 协议收尾。
- 流内 `event: error`(上游业务错误,如 `code=1005` 权益不足):`on_error` 回调冷却账号并注入 `event: error` + `data: [DONE]`(行 522-529)。

#### 2.2 工具调用截断是否检测

**不检测,且不需要检测。**(`_merge_tool_call_delta`,行 326-355)

- 工具调用 `arguments` 在 Buddy2api 层是 **纯字符串拼接**,不做 JSON 解析,自然不会因参数不完整抛错。
- 拼接完后直接把当前累计 `tool_calls` 通过 SSE 透传给上游(Harness)。是否完整闭合由上层消费者(agent)自行判断。
- 这是合理的设计:工具调用 JSON 闭合由模型侧保证;若上游 SOLO 真的传了不完整 JSON,透传也比静默丢弃更安全(消费者能拿到原始信号)。

#### 2.3 重试/冷却/日志

- **重试粒度:`MAX_ROTATE = 3`**(`constants.py:137`),单请求最多换 3 个账号。
- **轮转边界:仅在 2xx 前换号**(`_run_stream` 行 1132-1134,`_run_once` 同理)。一旦账号锁定、开始推流,**不会**因流中断而换号重试——这是设计选择,避免同一请求重复扣费。
- **冷却机制完整**:
  - `plan_limit` (1005):12h 硬冷却
  - `soft_rate` (429) / `not_found` (404):60s 短冷却
  - `session_dead` (401):标记 expired + 进入冷却池
  - 连续 3 次错误:10min 冷却
- **流中断会落日志**:`status_code=502, error_msg="upstream stream error"`,数据写入 `storage.database.record_request`,可查 `db.request_log`(如能查询)。

#### 2.4 模型配置(对 deepseek-v4-flash 的特殊处理)

`constants.py:51,57` 确认 `DeepSeek-V4-Flash` 与 `DeepSeek-V4-Flash-Official` 都是内置静态模型。

- `MODEL_RATES["DeepSeek-V4-Flash"] = 0.08`(`constants.py:112`),消耗倍率低。
- 通道内**没有针对该模型的特殊处理**(无长输出标志、无工具调用参数覆盖、无超时调整)。所有模型走统一路径。
- `DEFAULT_CONFIG = "glm-5.2"`(`constants.py:81`),`deepseek-v4-flash` 是用户显式选择的模型。

---

## 3. 需要修复的缺陷

### 3.1 代码缺陷

**未发现需要立即修复的 Buddy2api 代码 bug。**

工具调用合并、流中断、错误注入、日志落库、冷却状态机均按 spec 工作。`stream_to_openai` 在无 done 事件时仍写 [DONE] 兜底,确保 SSE 协议完整。

### 3.2 可选加固建议(非紧急,建议由后续 spec 决策)

下列项**不阻塞本次修复**,只在 stream 上有"自我保护"价值,留作 follow-up:

1. **流式重连 / 中断重试(可选)**:`_run_stream` 在 2xx 后不会因流中断换号重试。若想避免"SOLO 上游偶发断流导致用户看到不完整结果",可在 `stream_to_openai` 拿到首个 `output` chunk 后,缓存已发送字节,在 `httpx.HTTPError` 时由调用方重放——但代价是**可能产生重复扣费/重复工具调用**,需配合上游幂等 token 或允许重复的策略。当前实现选择"直接失败、不重放"是稳健的,**不建议改动**,除非上游支持幂等。

2. **超时兜底(可选)**:`_make_client` 行 60-62 设置流式 `read=None, connect=10.0`(对齐 Go 版"无读超时,仅首字节"),意味着长时间无 chunk 时不会主动断开。**当前实现是有意如此**,便于处理模型长思考 + 长输出场景。无需修复。

3. **Harness 层对策(建议在主对话侧讨论)**:
   - 若 Harness agent 在输出到 `</parameter>` 后被截断,**原因大概率是 Harness 客户端自己设置的超时/buffer 限制**或 **Harness 端模型生成达到 max_tokens 后被框架截断**(有时会被记为 `finish_reason=length`,而 Buddy2api 默认 `stop` 不会变更)。
   - 建议排查项:
     - Harness 端给 `/v1/chat/completions` 的 `max_tokens` / `stream` 配置
     - Harness 端 OpenAI 客户端读超时 / 流式 buffer 大小
     - DeepSeek-V4-Flash 在 Harness 端的系统提示或工具调用 schema 是否触发了异常

---

## 4. 修复建议汇总

按 spec §3 要求给出"具体改哪个文件、哪段逻辑、怎么改"。

### 4.1 推荐:**不改 Buddy2api 代码**

截断发生在 Harness 层,Buddy2api 的 traesolo 通道逻辑健全。修改 Buddy2api 既不能消除截断,也可能引入新回归。

### 4.2 主对话侧建议排查方向(由用户在主对话确认)

- 抓 Buddy2api 请求日志中同时间窗的记录(若日志可见),查看 `status_code` / `error_msg` / `finish_reason`:
  - 若大量 502 + "upstream stream error":说明 SOLO 端确实在断流,需要在 Harness 侧加重试或换模型。
  - 若全部 200 + finish_reason=stop:确认 Harness 层截断,排查 Harness 端的客户端配置。
- 与 Harness 侧确认:Buddy2api 实际返回的 SSE 流是否完整闭合(`data: [DONE]` 是否到达)。若到达但 Harness 仍截断到 `</parameter>`——明确是 Harness 侧处理 agent 工具调用包时的输出截断,与 Buddy2api 无关。
- 临时绕行方案:换用 `glm-5.2`(默认模型) 或其他模型对比,验证是否 deepseek-v4-flash 特有的输出形态。

---

## 5. 结论一句话

**截断发生在 Harness 层(agent 自身生成/截断),Buddy2api traesolo 通道无需修改。** 当前 traesolo 实现对流中断、工具调用合并、错误注入、日志落库、冷却状态机的处理均符合规范,且整个代码库不输出任何 XML 工具调用闭合标记——证据链闭合。

---

## 附录 A · 关键代码位置

| 关注点 | 文件 | 行号 |
|---|---|---|
| 流式 HTTP 客户端构造(无读超时) | `src/providers/traesolo/chat.py` | 59-65 |
| SSE 事件解析 | 同上 | 269-296 |
| `_SSEState` 累积器 | 同上 | 299-323 |
| 工具调用合并(`_merge_tool_call_delta`) | 同上 | 326-355 |
| 工具调用清理(`_clean_tool_calls`) | 同上 | 383-406 |
| 聚合(非流式) | 同上 | 413-460 |
| 流式 → OpenAI chunk 转换 | 同上 | 463-532 |
| 流中断兜底写 [DONE] | 同上 | 530-532 |
| 冷却状态机 `_Pool` | 同上 | 539-622 |
| 错误分类 `classify` | 同上 | 109-124 |
| `SoloStreamError`(流内业务错误) | 同上 | 127-136 |
| 非流式重试循环 | 同上 | 1009-1073 |
| **流式重试循环** | 同上 | **1091-1171** |
| 流中断 httpx.HTTPError 注入 error + [DONE] | 同上 | 1148-1152 |
| 流内事件错误 `on_error` | 同上 | 1137-1139 |
| 模型表(`DeepSeek-V4-Flash` 是内置) | `src/providers/traesolo/constants.py` | 46-79 |
| 冷却常量(`MAX_ROTATE=3`、`PLAN_COOLDOWN_S=12h` 等) | 同上 | 132-141 |
| FUNCTION=solo_work_lite, EP_CHAT=/api/agent/v3/llm_utils_chat | 同上 | 28-35 |

## 附录 B · 否定性证据

- `grep -r "<invoke\|</invoke\|<parameter\|</parameter" src/` → **0 匹配**。整个仓库不生成这些标记。
- `grep "finish_reason" src/providers/traesolo/chat.py` → Buddy2api 默认 `"stop"`(行 417),仅透传上游 done 事件的 finish_reason。流中断写 `"error"` 而非 `"length"`,因此即使上游 length 截断,Buddy2api 也会如实透传。
- `_clean_tool_calls` 与 `_merge_tool_call_delta` 都未对 `function.arguments` 做 JSON 完整性校验——这是有意为之,保证透传无损。

---

## 6. 完成状态

- 调查完成 ✓
- 结论:截断在 Harness 层,Buddy2api traesolo 通道无需修复
- 需主对话确认:Harness 侧配置排查(见 §4.2)