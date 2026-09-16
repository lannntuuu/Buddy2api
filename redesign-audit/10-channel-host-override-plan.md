# 每平台上游地址覆盖 · 方案 (Backend Feature)

> 目标:让管理员能在设置页为**每个平台通道**自定义上游地址(自建反代 / 内网镜像 / 换区),不再只有 workbuddy 一个。
> 类型:**后端 feature**(动 `storage`/`providers`/`gateway/routers/admin.py` + 前端设置页)。
> 红线:不改各平台协议常量默认值;不改路由/URL;不改账号数据;中文零 em-dash。

---

## 0. 背景与事实核查

之前核查确认:
- `backend_url`/`default_domain`/`timeout` 是**全局唯一**,只服务 workbuddy 通道(`accounts/auth_manager.py` 读取)。
- **其他平台上游地址硬编码在各自 provider 常量里**,形态各异:
  | 平台 | host 字段 | 数量 | 复杂度 |
  |------|-----------|------|--------|
  | GMI | `DEFAULT_BASE_URL` | 1 | 低(OpenAI 兼容,已有 `extra.base_url` 账号级覆盖) |
  | qwenwork | `GATEWAY` | 1 | 低(单 host) |
  | qclaw | `JPRX_GATEWAY` + `AIZONE_BASE` | 2 | 中 |
  | traesolo | `OAUTH_HOST` + `CONSOLE_HOST` + `AGENT_HOST` | 3 | 高 |
  | traework | 复用 trae_shared `AGENT_HOST`/`UG_HOST` | 2 | 高 |
- **已有先例**:GMI `extra.base_url`、traesolo `extra.api_host` 都是"账号级 host 覆盖",说明覆盖机制是成熟模式。

## 1. 设计

### 1.1 存储

settings 表新增一个 JSON 键 `channel_hosts`,结构:
```json
{
  "gmi":       {"base_url": "https://my-mirror.example.com/v1"},
  "qwenwork":  {"gateway": "https://my-gateway.example.com"},
  "qclaw":     {"jprx_gateway": "...", "aizone_base": "..."},
  "traesolo":  {"oauth_host": "...", "console_host": "...", "agent_host": "..."}
}
```
- 键 = channel id;值 = 该平台可覆盖的 host 字段名 → 新地址。
- 未配置的字段 = 用默认常量(零行为变化)。

### 1.2 读取帮助函数(核心)

新增 `providers/host_override.py`:
```python
"""Per-channel upstream host override, read from the settings table.

Admin can point a channel at a mirror / internal proxy without touching
provider constants. Unset fields fall back to the provider's default.
"""
from __future__ import annotations
from typing import Any
from storage import database as db

def channel_host(channel_id: str, field: str, default: str) -> str:
    """Resolve an upstream host for a channel, honouring admin override.

    field is one of the documented per-channel host fields (see
    CHANNEL_HOST_FIELDS). Returns default when unset.
    """
    raw = db.get_setting("channel_hosts", {}) or {}
    mapping = raw.get(channel_id) if isinstance(raw, dict) else None
    if isinstance(mapping, dict):
        val = str(mapping.get(field) or "").strip().rstrip("/")
        if val:
            return val
    return default

CHANNEL_HOST_FIELDS: dict[str, tuple[str, ...]] = {
    "gmi": ("base_url",),
    "qwenwork": ("gateway",),
    "qclaw": ("jprx_gateway", "aizone_base"),
    "traesolo": ("oauth_host", "console_host", "agent_host"),
}
```

### 1.3 各 provider 接入(只改 host 解析点,不动协议)

- **GMI** `chat.py _base_url()`:
  ```python
  from providers.host_override import channel_host
  def _base_url(account):
      extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
      host = str(extra.get("base_url") or account.get("domain") or
                 channel_host(CHANNEL_ID, "base_url", DEFAULT_BASE_URL)).rstrip("/")
      return host or DEFAULT_BASE_URL
  ```
- **qwenwork** `chat.py`/`token.py`/`__init__.py` 的 `GATEWAY` 引用处:改为 `channel_host(CHANNEL_ID, "gateway", GATEWAY)`。
- **qclaw** `chat.py` 的 `AIZONE_BASE`、`jprx.py` 的 `JPRX_GATEWAY`:改为 `channel_host(...)`。
- **traesolo** `token.py _oauth_base()`、`chat.py` 的 `AGENT_HOST`、`login.py` 的 `OAUTH_HOST`/`CONSOLE_HOST`:改为 `channel_host(...)`。
- **traework**:复用 trae_shared host,接入点同 traesolo(共享 host 覆盖)。

### 1.4 前端设置页

`web/js/pages/settings.js` 的"后端参数"卡片,从"只 workbuddy 三字段"改为**每平台一个可折叠区**,展示该平台可覆盖的 host 字段(从后端 `/admin/settings` 返回的 `channel_hosts` 读),保存时 PUT 回 `channel_hosts`。

### 1.5 后端 admin.py

- `allowed_settings` 加 `"channel_hosts"`。
- PUT 时校验 `channel_hosts` 结构:必须是 dict,每个 channel 的字段必须在 `CHANNEL_HOST_FIELDS` 白名单内,值必须 `https://` 开头(与 backend_url 同规则)。

## 2. 范围与风险(诚实声明)

**分阶段实施,避免一次性全量高风险改动:**

- **Phase A(本次)**:GMI + qwenwork(单 host 平台)+ 后端存储/读取/校验 + 前端设置页 UI。风险低,覆盖"最常见的镜像需求"。
- **Phase B(后续)**:qclaw / traesolo / traework(多 host 平台)。风险中高(协议核心 host,改错直接坏),需要更多测试。

**为什么分阶段**:qclaw 的 `JPRX_GATEWAY` 是签名/业务网关、`AIZONE_BASE` 是 chat 端点,traesolo 三个 host 各有职责,一次性全改容易引入回归。先做单 host 平台验证机制,再扩展。

## 3. 验证

- 单测:`pytest -q tests/test_core.py`(确认无回归)。
- 新增测试:`tests/test_host_override.py`:
  - `channel_host()` 未设置时返回默认
  - 设置后返回覆盖值
  - 非法结构被 admin PUT 拒绝
- ESM 校验:`pytest -q tests/test_web_assets.py`。
- 手动:设置页给 GMI 填镜像地址 → 保存 → 请求走镜像。

## 4. 不做的事

- **不改**各平台协议常量默认值(默认行为零变化)。
- **不做**账号级 host 覆盖的迁移(已有 GMI/traesolo 先例,保留)。
- **不做** host 字段的动态发现(每平台字段白名单写死,安全)。

## 5. Commit 计划

```
Phase A:
  feat(providers): per-channel upstream host override (gmi, qwenwork)
  feat(web): per-channel host override UI in settings
  test(providers): host override resolution + validation
```
