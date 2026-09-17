# 11115「prompt is too long」断流问题 · 诊断报告

日期：2026-09
对象：prod 部署（`C:\Usr\Code\etc\Buddy2api-prod`，端口 **8788**，DB `data/codebuddy_gateway.db`）
触发现象的客户端：DeepSeek Harness (DSH)，`provider:"buddy"` → `http://127.0.0.1:8788/v1`

---

## 一、一句话结论

**这不是项目转发层的问题，也不是模型上下文"只剩 100k"。**

真正的因果链是：**DSH 对话框上下文无界累积到了 75 万~116 万 token，超过所有可用模型的实际承载 → 触发 workbuddy 上游一条"写死的"11115 错误文案（`100001 > 100000`，恒成立，不反映真实 token、也不随模型配置 1M 自适应）→ prod 网关无 11115 容错，直接透传给 DSH → 重试无果、compaction 因带全量历史同样超限失败 → 「无法继续/无法压缩」死锁。**

> **最关键的新发现**：workbuddy 的 11115 `msg` 是**固定字面量**。无论实际输入多少（实测 ~751k，曾有 1.16M）、无论模型上限配多少（glm=1M，能跑 697k），它都恒定返回 `100001 tokens > 100000 maximum`。hy3 则是更早版本的固定文案 `input length too long`。这 "100000" 与真实上下文和模型上限都无关。

---

## 二、决定性证据链

### 1. 同一部署、同一模型、同一客户端，上下文额度大幅波动 → 证否「转发放大」

以下是 prod DB 里 `glm-5.3-flash` 的历史记录：

| 时间 | 账号 | prompt_tokens | 结果 |
|---|---|---|---|
| 2026-09-09 22:03 | 图宽 (id=1) | **697,159** | 200 ✅ |
| 2026-09-16 14:22~14:27 | 图宽 + lannntuuu | **100,001** | 400 11115 ❌ |
| 2026-09-16 14:46~14:47 | dsh | 91k ~ 93k | 200 ✅ |

**推理**：如果转发层会放大/改写 body 导致超限，那么 697k 成功早就该失败，且不会出现「697k → 100k → 93k」如此大幅的时间波动。同一段转发代码要同时放过 697k 又拒 100k，只有一种解释——**上限不是固定的，而是账号当刻的配额在变**。转发是原样透传（`build_backend_body` 仅 role 别名 / model alias / 注入输出 `max_tokens`，不重发不拼装历史）。

> 补充：同一天 glm-5.3-flash 在 09-09 成功 697k，而 09-16 14:27 失败了。客户端的 100001→100000 误差只差 1 token，说明正是"上限 100k"即时生效。

### 2. 触发器是 deepseek-v4.1-flash 的 429 限频

09-16 13:33 → 14:08 之间，prod DB 记录了大量 `status_code=429`、`code=6004`、「使用次数超出频率限制（恢复 2026-09-16 16:51 / 21:31 UTC+8）」，对象是 `deepseek-v4.1-flash`，**两个账号轮流命中**。DSH 在 session 里因此做了 `model/selection` 切到 glm，与你的操作一致。

### 3. 切到 glm 时，workbuddy 两个账号同时被压到 100k

09-16 14:22~14:27 的 11115 记录中，`account_id` 在 **1（图宽）与 6（lannntuuu）之间交替**——两个都是 active、都被选中、都 11115。**账号 failover（即使有）在此刻也救不了**，因为两个账号当刻限额一致。

### 4. prod 代码缺 11115 容错 → 断流、无法压缩

在 prod `src/upstream/proxy.py` / `compaction.py` 里**搜不到** `_is_oversize_error` / `_is_11115_error` / `_self_heal_oversize` / `11115` / `context_length_exceeded` —— 它仍是旧版。

当前 prod 对 400 的处理：
- 400 不在 `RETRYABLE_STATUS`（{408,409,425,429,500,502,503,504}）→ **不重试、不换账号**；
- `_apply_model_limits` 用**管理员配置**做预检上限。而 prod 配置里 `glm-5.3-flash = 1000000`（1M），所以预检**从不拦截**；
- 于是 11115 直接被 `_safe_err`/`_err_sse_event` **透传给 DSH**。

DSH 端（session 记录）：
- turn 133/134 连续 `FINISH error`，`reason.kind=error`，`code=CONTEXT_WINDOW_EXCEEDED`；
- DSH 尝试 `compaction`（带全量历史让模型做总结）→ **compaction 请求本身也超 100k → 11115 → 失败**（`compaction/end` 带 error）；
- 于是「无法继续对话、无法压缩上下文」的死锁成立。

### 5. 相关旧事：hy3 也有同样的「账号额度」型 11115

pro DB：09-08、09-10 多起 `hy3` 的 11115 `input length too long`（`extError.code` 为 400003/context_length_exceeded）。同一模型 hy3 配置上限 192000，但同样只在特定时刻超。与 glm 同根——**账号配额型超限，而非模型固定上限**。

---

## 三、配置对照

| 项 | prod（8788，经手者） | dev（8787，本分支） |
|---|---|---|
| 模型清单 | 含 `glm-5.3-flash`、`deepseek-v4.1-flash` | 不含（旧清单 hy3/hy4/hy3-x/deepseek-v4-flash） |
| `workbuddy.max_input_tokens` | 1048576（1M） | 1048576（1M） |
| `workbuddy.max_input_tokens_by_model` | `{"hy3":192000,"glm-5.3-flash":1000000}` | `{"hy3":192000,"hy3-x":192000}` |
| 11115 识别/自愈 | **无** | 有（bbcf8bc，但**未合并进 prod**) |
| DSH 连接 | 是（client='dsh' 大量记录） | 是 |

> 注：admin 把 `glm-5.3-flash` 上限配成 1000000（1M）是**合理的**——它确实能到 697k。问题不在配置，而在 prod 运行时代码没有"配额超限后可恢复"的处理。

---

## 四、DSH 侧为什么「以为 1M 却失败」

DSH 的 buddy provider 配置：

```yaml
glm-5.3-flash:
  contextWindow: 1000000        # 声称 1M
  input: [text, image]
deepseek-v4.1-flash:
  contextWindow: 1000000
```

DSH 据此认为模型能装 1M，**从不主动精简**。但上游在账号配额被压到 100k 时不认这个声明的窗口。这是 **DSH 的 contextWindow 声明（静态假象） vs 上游账号实际配额（动态）** 之间的矛盾，转发层只是被夹在中间的那层。

---

## 五、给不同角色的结论

- **给「转发层是不是 bug」的判断**：不是。证据是 697k/100k/93k 的波动 + 代码透传逻辑 + **本地 Qwen 直连实验**（见下）。
- **给「为什么切 GLM 就断」的判断**：因为对话框上下文已达 ~751k（甚至 1.16M），workbuddy 对"超大输入"统一返回**写死的** `100001 > 100000` 固定错误，与 glm 配置上限 1M、真实能力 697k 无关。
- **给「为什么无法压缩」**：compaction 请求同属超大输入（带全量 751k 历史），触发同一条 11115 固定 400，被挡下。

### 决定性交叉验证：本地 Qwen 直连实验

同一对话框切到本地 vLLM `Qwen3.8-27B-FP8`（DSH `provider:"local"`，**不经过本网关**），返回：

```
BadRequestError: The input (1159416 tokens) is longer than the model's context length (196608 tokens).  code=400
```

三点印证：
1. **真实上下文确实累积到 115 万+ token**（独立于网关路径被告知），绝非网关转发放大。
2. **两家实现差异**：vLLM 如实报精确 `1159416 > 196608`；workbuddy 只给固定 `100001 > 100000` → 证明 11115 是"写死文案"的实现，不诚实报告数值。
3. **Qwen 196k < 它声称的 262k** → 即使 DSH 信任的 contextWindow 也有水分，进一步动摇"按声明窗口认为能装 1M"。

---

## 六、可选后续（本报告不涉及 prod 改动）

核心方向已从「11115 容错」收敛为「**让超大对话框更早压实（compact），别等撞墙**」，因为对话已可到 116 万 token，远超一切模型。

1. **网关侧兜底紧凑一层**（本项目可做）：对超大请求（如估算 input 超过某阈值）在转发前先行做一版轻量紧凑/截断，把历史压到模型可承载量再转发，帮 DSH 兜底；失败的业务仍让客户端自行精简。这是**对「无法压缩」死锁的直接解**。
2. **让 /v1/models 回传真实可承载上下文**（本项目可做）：把各模型的真实 context_window（如 Qwen 196k、glm 按账号当刻配额）回报给 DSH，让 DSH 至少按真实窗口判断，而不是信静态 1M/262k。
3. **DSH 侧更早、更激进紧凑**（DSH 侧）：DSH 目前信任 contextWindow 声明、从不提前紧凑，等到撞墙；它需要更早按真实量紧凑。这是最有效但跨项目。
4. **prod 融入 11115 容错（若仍要）**：识别 11115 → 武装 + 激进精简 → 原样重试一次；但**注意 11115 文案固定，精简未必能绕过**（真实 751k 就是 >100k 门槛），所以这只能缓解、不治本；不能解决「conversation 早涨到 116 万」的根。

> 结论迁移：此前以为「切 GLM 时账号配额恰好压到 100k」（动态）。交叉验证后修正为——**全局都超，问题在主于「上下文涨太满 + 客户端不提前紧凑」**，11115 只是 100k 这条固定文案的一次体现。

---

*报告基于：DSH session 日志（`dsh-session-session-71f274ac....zip`）、prod DB（`data/codebuddy_gateway.db`，只读查询）、prod 源码检索、本地 vLLM Qwen 交叉实验。所有落库证据均为请求错误原始透传体。*