# 第三方协议参考

Buddy2api 2.0 各通道的出站指纹与请求格式为 **本仓库原创实现**。下列仓库仅用于对照公开协议行为（URL、头字段名、鉴权流程），**不是源代码依赖**，也未复制其实现。

无 LICENSE 文件的仓库默认未授予复制权。本项目不 vendor 其 Go/TS 树，也不复制其中的密钥常量。

## WorkBuddy / CodeBuddy

| 仓库 | 许可（GitHub SPDX） | 用途 |
|---|---|---|
| [Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api) | 无 LICENSE（README 自称 MIT） | 签到路径、CLI 头对照 |
| [orangeboyChen/codebuddy2api](https://github.com/orangeboyChen/codebuddy2api) | MIT | B3 / stainless / IDE 头字段名 |
| [HanHan666666/codebuddy2openai](https://github.com/HanHan666666/codebuddy2openai) | MIT | 本机 `.info` 布局 |
| [wugxxx/codebuddy2api](https://github.com/wugxxx/codebuddy2api) | 无 LICENSE（README 自称 MIT） | `x-codebuddy-request` 等字段名 |
| [xueyue33/codebuddy2api](https://github.com/xueyue33/codebuddy2api) | 无 | conversation / stainless 字段名 |

## QClaw

| 仓库 | 许可 | 用途 |
|---|---|---|
| [Sliverkiss/qclaw2api](https://github.com/Sliverkiss/qclaw2api) | 无 LICENSE（README 自称 MIT） | jprx / aizone 的 host 与头名。HMAC 密钥从官方客户端提取，不从该仓复制 |

## QwenWork

| 仓库 | 许可 | 用途 |
|---|---|---|
| [wpy030414/xrl-router-plugin-qwenwork](https://github.com/wpy030414/xrl-router-plugin-qwenwork) | 无 | 网关路径、COSY 头名、`auth-v2.dat` 布局。RSA PEM 从官方 0.1.8 客户端提取 |

## QoderWork CN（2.0.0 非必做）

| 仓库 | 许可 | 用途 |
|---|---|---|
| [Sliverkiss/qoderwork2api](https://github.com/Sliverkiss/qoderwork2api) | 无 LICENSE（README 自称 MIT） | host / 签到路径级事实 |

## 明确不使用

- `Sliverkiss/ds2api`（AGPL-3.0）
- Trae / Lingma / Qoder CN 作为产品通道（见 `docs/design/multi-channel-v2.md`）
