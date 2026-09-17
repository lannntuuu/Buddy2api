# context-overflow-troubleshoot

「11115 / prompt is too long」上下文超限断流问题的定位资料包。

## 内容

| 文件 | 说明 | 是否入库 |
|---|---|---|
| `11115-context-exceeded-diagnosis.md` | 诊断报告（最终结论详见下方"结论摘要"） | 入库 |
| `gateway_log_inspect.py` | 可复用的只读网关日志分析脚本 | 入库 |
| `session-71f274ac.jsonl` | DSH session 原始证据日志（约 24MB） | **不入库**，见下 |

## 原始 session 日志位置

大文件 `session-71f274ac.jsonl`（源自
`dsh-session-session-71f274ac-c753-43cf-ba85-c8e3ebcd8847.zip`）体积较大，
**不提交进 git**，存放在 git 忽略的临时目录：

- 仓库内相对路径：`.tmp/context-overflow-evidence/session-71f274ac.jsonl`
  （`.tmp/` 已在 `.gitignore` 忽略）

> 若该文件丢失或需重新获取：原始 zip 位于
> `D:\User\Download\dsh-session-session-71f274ac-c753-43cf-ba85-c8e3ebcd8847.zip`，
> 解压后是单个 `session.jsonl`。本目录下诊断报告与其内容依据即该文件。

## 分析脚本用法

```bash
# 查 dev 库
python docs/context-overflow-troubleshoot/gateway_log_inspect.py

# 查 prod 库（只读，自动复制到 .tmp 再查）
python docs/context-overflow-troubleshoot/gateway_log_inspect.py \
    -db "C:/Usr/Code/etc/Buddy2api-prod/data/codebuddy_gateway.db"
```

脚本只读复现报告中的关键证据：11115 固定文案签名、各模型历史最大成功
prompt_tokens、429 限频前导、workbuddy 上下文限额配置、账号分布。

## 结论摘要（详见诊断报告）

1. **不是项目转发层的问题**：本地 Qwen 直连（不经网关）也报出真实
   `1159416 > 196608`，证明上下文确实累积到百万级，非网关转发放大。
2. **workbuddy 的 11115 是固定文案**：glm-5.3-flash 恒定返回
   `prompt is too long: 100001 > 100000 maximum`（6 次），hy3 恒定返回
   `input length too long`（9 次）——与真实 token（约 75~116 万）和模型
   实际上限（glm 可达 697k、deepseek 750k）都不符。
3. **根因**：DSH 对话框上下文无界累积到 75 万~116 万 token，超过所有模型；
   DSH 信任静态 contextWindow（1M/262k）从不提前紧凑；撞墙后 compaction
   因带全量历史同样超限而失败 → "无法继续 / 无法压缩"死锁。
4. **建议方向**：网关侧兜底紧凑一层 / `/v1/models` 回报真实可承载上下文 /
   DSH 侧更早、更激进紧凑（跨项目）。