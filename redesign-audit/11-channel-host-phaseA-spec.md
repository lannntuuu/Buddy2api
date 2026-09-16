# 每平台上游地址覆盖 · Phase A · 可执行实施规范 (EXECUTION SPEC)

> 本文件是**自包含、可直接由 subagent 执行**的实施规范。subagent **不需要回看主对话**。
> 实施 `redesign-audit/10-channel-host-override-plan.md` 的 **Phase A**:GMI + qwenwork 的单 host 覆盖 + 后端存储/读取/校验 + 前端设置页 UI。
> **不实施** Phase B(qclaw/traesolo/traework 多 host)。
> 红线:不改各平台协议常量默认值;不改路由/URL;不改账号数据;中文零 em-dash (——)。

---

## 0. 目标

让管理员能在设置页为 GMI、qwenwork 两个平台自定义上游地址(镜像/反代/换区)。未配置时行为与现在完全一致。

## 1. 后端改动

### 1.1 新增 `providers/host_override.py`

```python
"""Per-channel upstream host override, read from the settings table.

Admin can point a channel at a mirror / internal proxy without touching
provider constants. Unset fields fall back to the provider's default.
"""
from __future__ import annotations
from storage import database as db

# Field whitelist per channel. Only these keys are accepted in the
# `channel_hosts` settings blob; anything else is rejected by the admin
# route. Phase A covers single-host channels only.
CHANNEL_HOST_FIELDS: dict[str, tuple[str, ...]] = {
    "gmi": ("base_url",),
    "qwenwork": ("gateway",),
}

def channel_host(channel_id: str, field: str, default: str) -> str:
    """Resolve an upstream host for a channel, honouring admin override.

    field must be one of CHANNEL_HOST_FIELDS[channel_id]. Returns the
    admin-configured value when set, otherwise `default`.
    """
    raw = db.get_setting("channel_hosts", {}) or {}
    mapping = raw.get(channel_id) if isinstance(raw, dict) else None
    if isinstance(mapping, dict):
        val = str(mapping.get(field) or "").strip().rstrip("/")
        if val:
            return val
    return default
```

> 说明:`db.get_setting` 已有(json 解析 + 默认值)。若 `channel_hosts` 未设置,`raw={}` → 直接返回 default,零行为变化。

### 1.2 GMI 接入(`providers/gmi/chat.py`)

`_base_url()`(约 89-92 行)改为优先用全局覆盖,再回退到账号级覆盖,再默认:

```python
from providers.host_override import channel_host  # 顶部 import

def _base_url(account: dict) -> str:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    host = str(
        extra.get("base_url")
        or account.get("domain")
        or channel_host(CHANNEL_ID, "base_url", DEFAULT_BASE_URL)
    ).rstrip("/")
    return host or DEFAULT_BASE_URL
```

> 覆盖优先级:账号 `extra.base_url` > 账号 `domain` > **全局 `channel_hosts.gmi.base_url`** > `DEFAULT_BASE_URL`。(账号级优先,管理员全局覆盖是兜底。)

### 1.3 qwenwork 接入

`GATEWAY` 通过 `from providers.qwenwork.constants import GATEWAY` 引入,共 3 处使用:

1. `providers/qwenwork/chat.py:45` `chat_url()`:
   ```python
   from providers.host_override import channel_host  # 顶部 import
   def chat_url() -> str:
       return f"{channel_host(CHANNEL_ID, 'gateway', GATEWAY)}{CHAT_PATH}?{CHAT_QUERY}"
   ```
2. `providers/qwenwork/token.py:59`(refresh 调用):
   ```python
   from providers.host_override import channel_host
   # 把 f"{GATEWAY}{REFRESH_PATH}" 改为 f"{channel_host(CHANNEL_ID, 'gateway', GATEWAY)}{REFRESH_PATH}"
   ```
3. `providers/qwenwork/__init__.py:105`(account-context):
   ```python
   from providers.host_override import channel_host
   # 把 f"{GATEWAY}{ACCOUNT_CONTEXT_PATH}..." 改为 f"{channel_host(CHANNEL_ID, 'gateway', GATEWAY)}{ACCOUNT_CONTEXT_PATH}..."
   ```

> 每个文件只需在引用处调用 `channel_host(...)`,不要改 constants.py 的 `GATEWAY` 默认值。

### 1.4 后端 admin 路由(`gateway/routers/admin.py`)

`/admin/settings` PUT(约 918-941 行):
1. `allowed_settings` 加 `"channel_hosts"`:
   ```python
   allowed_settings = {"backend_url", "default_domain", "timeout", "channel_hosts"}
   ```
2. 校验 `channel_hosts`(在 `for k, v in data.items()` 之前加一段):
   ```python
   if "channel_hosts" in data:
       ch = data["channel_hosts"]
       if not isinstance(ch, dict):
           raise HTTPException(status_code=400, detail="channel_hosts must be an object")
       from providers.host_override import CHANNEL_HOST_FIELDS
       for cid, fields in ch.items():
           if cid not in CHANNEL_HOST_FIELDS:
               raise HTTPException(status_code=400, detail=f"Unsupported channel: {cid}")
           if not isinstance(fields, dict):
               raise HTTPException(status_code=400, detail=f"channel_hosts[{cid}] must be an object")
           unknown = set(fields) - set(CHANNEL_HOST_FIELDS[cid])
           if unknown:
               raise HTTPException(status_code=400, detail=f"Unsupported host fields: {', '.join(sorted(unknown))}")
           for f, v in fields.items():
               if not v:
                   continue
               val = str(v).strip().rstrip("/")
               if not val.startswith("https://"):
                   raise HTTPException(status_code=400, detail=f"{cid}.{f} must use HTTPS")
               fields[f] = val
   ```

## 2. 前端改动(`web/js/pages/settings.js`)

"后端参数"卡片从"只 workbuddy 三字段"扩展为:**保留 workbuddy 三字段 + 新增"平台上游地址覆盖"区**,展示 GMI/qwenwork 的可覆盖 host 字段。

### 2.1 数据结构

`load()` 已把 `/admin/settings` 合并进 `s`。`s.channel_hosts` 来自后端。前端编辑用 reactive:

```js
const hostOverrides = reactive({gmi:{base_url:''}, qwenwork:{gateway:''}});
// load() 后:把 s.value.channel_hosts 填进 hostOverrides
function fillHosts(){
  const ch = s.value.channel_hosts||{};
  hostOverrides.gmi.base_url = ch.gmi?.base_url||'';
  hostOverrides.qwenwork.gateway = ch.qwenwork?.gateway||'';
}
```

### 2.2 保存

`save()` 的 PUT body 加 `channel_hosts`:
```js
const body = {backend_url:..., default_domain:..., timeout:..., channel_hosts: {
  gmi: {base_url: hostOverrides.gmi.base_url.trim()},
  qwenwork: {gateway: hostOverrides.qwenwork.gateway.trim()},
}};
// 空值后端会跳过(见 1.4 校验的 `if not v: continue`)
```

### 2.3 template

在"后端参数"卡片内、workbuddy 字段下方新增一个分隔区:
```html
<div class="field"><label>GMI Cloud Base URL</label>
  <input v-model="hostOverrides.gmi.base_url" placeholder="留空使用默认 https://api.gmi-serving.com/v1"/>
  <div class="hint">自定义 GMI 上游地址(镜像/反代)。留空 = 默认。</div></div>
<div class="field"><label>QwenWork 网关</label>
  <input v-model="hostOverrides.qwenwork.gateway" placeholder="留空使用默认 https://gateway.qwenwork.cn"/>
  <div class="hint">自定义 QwenWork 网关地址。留空 = 默认。</div></div>
```
> 放在 `form-grid` 里,复用现有 `.field` 样式。文案用中文,零 em-dash。

## 3. 测试

新增 `tests/test_host_override.py`:
```python
"""Tests for per-channel upstream host override."""
import pytest
from providers.host_override import channel_host, CHANNEL_HOST_FIELDS
from storage import database as db

def test_default_when_unset(monkeypatch):
    monkeypatch.setattr(db, "get_setting", lambda k, d=None: d)
    assert channel_host("gmi", "base_url", "https://default") == "https://default"
    assert channel_host("qwenwork", "gateway", "https://default") == "https://default"

def test_override_when_set(monkeypatch):
    monkeypatch.setattr(db, "get_setting",
        lambda k, d=None: {"gmi": {"base_url": "https://mirror.example.com/v1"}})
    assert channel_host("gmi", "base_url", "https://default") == "https://mirror.example.com/v1"
    assert channel_host("qwenwork", "gateway", "https://default") == "https://default"

def test_channel_host_fields_phase_a():
    assert CHANNEL_HOST_FIELDS == {"gmi": ("base_url",), "qwenwork": ("gateway",)}
```

运行:`pytest -q tests/test_host_override.py tests/test_core.py tests/test_web_assets.py`

## 4. 交付后自检

- [ ] `pytest -q tests/test_host_override.py` → 3 passed
- [ ] `pytest -q tests/test_core.py` → 无回归(12 个 pre-existing 失败忽略)
- [ ] `pytest -q tests/test_web_assets.py` → 14 passed(ESM)
- [ ] `git grep -n "—" -- web/ providers/` → 0(中文零 em-dash;英文注释里的 em-dash 允许)
- [ ] 只 add 明确文件

## 5. Commit 纪律(可拆 2-3 颗)

```
1. feat(providers): per-channel upstream host override (gmi, qwenwork)
   → add: providers/host_override.py, providers/gmi/chat.py,
          providers/qwenwork/chat.py, providers/qwenwork/token.py,
          providers/qwenwork/__init__.py, gateway/routers/admin.py,
          tests/test_host_override.py
2. feat(web): per-channel host override UI in settings
   → add: web/js/pages/settings.js
```

每颗:`git commit -m "..."` → `git push origin refactor/web-console-ia` → 在 `redesign-audit/05-implementation-log.md` 追加 Phase A 段。

## 6. 兜底

- 若某处 `GATEWAY`/`DEFAULT_BASE_URL` 引用点与 spec 描述不符(行号可能漂移):按**符号名**全局 grep 定位,不要死磕行号。
- 若 `db.get_setting` 的行为与 spec 假设不符(如不解析 JSON):以实际代码为准,调整 `channel_host()` 使其正确读取。
- 若 pytest 出现与本次改动**无关**的新失败:停下报告,不要乱修。
- 中文 UI 文案零 em-dash;英文 commit/注释可含。
