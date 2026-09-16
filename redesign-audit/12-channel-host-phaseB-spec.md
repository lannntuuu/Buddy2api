# 每平台上游地址覆盖 · Phase B · 可执行实施规范 (EXECUTION SPEC)

> 本文件是**自包含、可直接由 subagent 执行**的实施规范。subagent **不需要回看主对话**。
> 实施 `redesign-audit/10-channel-host-override-plan.md` 的 **Phase B**:qclaw / traesolo / traework 多 host 平台的上游地址覆盖。
> **Phase A 已完成**(GMI + qwenwork,`providers/host_override.py` 已存在)。
> 红线:不改各平台协议常量默认值;不改路由/URL;不改账号数据;中文零 em-dash (——)。

---

## 0. 目标

把 Phase A 建立的 `channel_host()` 机制扩展到 qclaw / traesolo / traework。未配置时行为与现在完全一致。

## 1. 前置确认(先读,勿跳过)

- `providers/host_override.py` 已存在(Phase A 建的),含 `CHANNEL_HOST_FIELDS` 和 `channel_host()`。
- **本次要扩展 `CHANNEL_HOST_FIELDS`**,加入三个平台的白名单。
- **关键设计**:traework 与 traesolo **共享** `trae_shared.AGENT_HOST`/`UG_HOST`,但覆盖必须**按 channel 独立**——traework 用 `channel_host("traework", ...)`,traesolo 用 `channel_host("traesolo", ...)`,互不影响(默认值相同,覆盖各自生效)。

## 2. 后端改动

### 2.1 `providers/host_override.py` 扩展 `CHANNEL_HOST_FIELDS`

```python
CHANNEL_HOST_FIELDS: dict[str, tuple[str, ...]] = {
    "gmi": ("base_url",),
    "qwenwork": ("gateway",),
    "qclaw": ("jprx_gateway", "aizone_base"),
    "traesolo": ("oauth_host", "console_host", "agent_host"),
    "traework": ("agent_host", "ug_host"),
}
```

> `channel_host()` 函数本身**不用改**,它已支持任意 channel_id + field。

### 2.2 qclaw 接入

**`providers/qclaw/jprx.py`**(约 95 行):
```python
from providers.host_override import channel_host  # 顶部 import
# 把 url = f"{JPRX_GATEWAY}/data/{cmd}/forward"
# 改为 url = f"{channel_host(CHANNEL_ID, 'jprx_gateway', JPRX_GATEWAY)}/data/{cmd}/forward"
```
> 确认 jprx.py 顶部已 import `CHANNEL_ID`(从 constants)。若没有,补上。

**`providers/qclaw/chat.py`**(3 处 `AIZONE_BASE`,约 148/213/288 行):
```python
from providers.host_override import channel_host  # 顶部 import
# 每处 f"{AIZONE_BASE}/chat/completions"
# 改为 f"{channel_host(CHANNEL_ID, 'aizone_base', AIZONE_BASE)}/chat/completions"
```
> 若 chat.py 未 import `CHANNEL_ID`,补上(它应该已有,因为要记日志)。

### 2.3 traesolo 接入

**`providers/traesolo/token.py`**:
- 约 48-49 行 `_oauth_base()`:
  ```python
  from providers.host_override import channel_host  # 顶部 import
  def _oauth_base(account):
      host = str(extra_of(account).get("api_host") or
                 channel_host(CHANNEL_ID, "oauth_host", OAUTH_HOST)).rstrip("/")
      return host or OAUTH_HOST
  ```
- 约 174 行 `base = (host or OAUTH_HOST).rstrip("/")` → 同样在 `or` 链里插 `channel_host(...)`(若该函数无 account 参数,则直接用 `channel_host(CHANNEL_ID, "oauth_host", OAUTH_HOST)` 替代裸 `OAUTH_HOST`)。
- 约 278 行 `f"{CONSOLE_HOST}/authorization?{encoded}"` → `f"{channel_host(CHANNEL_ID, 'console_host', CONSOLE_HOST)}/authorization?{encoded}"`。

**`providers/traesolo/chat.py`**(约 722/906 行 `AGENT_HOST`):
```python
from providers.host_override import channel_host  # 顶部 import
# f"{AGENT_HOST}{EP_MODELS}" → f"{channel_host(CHANNEL_ID, 'agent_host', AGENT_HOST)}{EP_MODELS}"
# f"{AGENT_HOST}{EP_CHAT}"  → f"{channel_host(CHANNEL_ID, 'agent_host', AGENT_HOST)}{EP_CHAT}"
```

**`providers/traesolo/login.py`**(约 148/163/185/197 行 `OAUTH_HOST`):
```python
from providers.host_override import channel_host  # 顶部 import
# 把裸 OAUTH_HOST 的引用改为 channel_host(CHANNEL_ID, 'oauth_host', OAUTH_HOST)
# 注意:login.py 里 OAUTH_HOST 同时用于 exchange() 调用和写入 extra.api_host。
#   - 调用 exchange() 的 host 参数:用 channel_host(...)
#   - 写入 extra 的 api_host:用 channel_host(...)(保持登录后账号用同一 host)
```

**`providers/traesolo/store.py`**(约 81 行 `OAUTH_HOST`):
```python
from providers.host_override import channel_host  # 顶部 import
# api_host = _first(...) or OAUTH_HOST
# → api_host = _first(...) or channel_host(CHANNEL_ID, 'oauth_host', OAUTH_HOST)
```

### 2.4 traework 接入

traework 复用 trae_shared 的 `AGENT_API`/`UG_API`,且内部已有账号级 `extra.host` 覆盖(quota.py/token.py/store.py)。

**`providers/traework/chat.py`**(约 269 行 `AGENT_API`):
```python
from providers.host_override import channel_host  # 顶部 import
# session_url = f"{AGENT_API}{SESSIONS_PATH}"
# → session_url = f"{channel_host(CHANNEL_ID, 'agent_host', AGENT_API)}{SESSIONS_PATH}"
```

**`providers/traework/quota.py`**(约 23 行):
```python
from providers.host_override import channel_host  # 顶部 import
# return str(extra.get("host") or UG_API).rstrip("/") or UG_API
# → return str(extra.get("host") or channel_host(CHANNEL_ID, "ug_host", UG_API)).rstrip("/") or UG_API
```

**`providers/traework/token.py`**(约 57-58 行):
```python
from providers.host_override import channel_host  # 顶部 import
# host = str(extra.get("host") or UG_API).rstrip("/")
# → host = str(extra.get("host") or channel_host(CHANNEL_ID, "ug_host", UG_API)).rstrip("/")
```

**`providers/traework/store.py`**(约 90/130 行 `UG_API`):
```python
from providers.host_override import channel_host  # 顶部 import
# host = str(document.get("host") or UG_API) → str(document.get("host") or channel_host(CHANNEL_ID, "ug_host", UG_API))
# "host": body.get("host") or UG_API → body.get("host") or channel_host(CHANNEL_ID, "ug_host", UG_API)
```

> 覆盖优先级(与 GMI 一致):账号 `extra.host` > 全局 `channel_hosts.<channel>.ug_host` > `UG_API`。

### 2.5 后端 admin 路由校验

Phase A 已加了 `channel_hosts` 校验,`CHANNEL_HOST_FIELDS` 扩展后**自动覆盖**新平台(校验逻辑读 `CHANNEL_HOST_FIELDS`,无需改 admin.py)。**确认即可,不改 admin.py。**

## 3. 前端改动(`web/js/pages/settings.js`)

在 Phase A 的 GMI/QwenWork 字段下方,新增三个平台的 host 字段区。`hostOverrides` reactive 扩展:

```js
const hostOverrides = reactive({
  gmi:{base_url:''},
  qwenwork:{gateway:''},
  qclaw:{jprx_gateway:'', aizone_base:''},
  traesolo:{oauth_host:'', console_host:'', agent_host:''},
  traework:{agent_host:'', ug_host:''},
});
```

`fillHosts()` 对应填充;`save()` 的 PUT body 对应扩展(空值后端跳过)。

template 新增字段(中文文案,零 em-dash):
```html
<!-- QClaw -->
<div class="field"><label>QClaw JPRX 网关</label><input v-model="hostOverrides.qclaw.jprx_gateway" placeholder="留空使用默认 https://jprx.m.qq.com"/><div class="hint">业务/签名网关。留空 = 默认。</div></div>
<div class="field"><label>QClaw AIZone 地址</label><input v-model="hostOverrides.qclaw.aizone_base" placeholder="留空使用默认 https://mmgrcalltoken.3g.qq.com/aizone/v1"/><div class="hint">chat 端点。留空 = 默认。</div></div>
<!-- Trae SOLO -->
<div class="field"><label>Trae SOLO OAuth 地址</label><input v-model="hostOverrides.traesolo.oauth_host" placeholder="留空使用默认 https://api.trae.com.cn"/><div class="hint">ExchangeToken / GetUserInfo。留空 = 默认。</div></div>
<div class="field"><label>Trae SOLO 登录页</label><input v-model="hostOverrides.traesolo.console_host" placeholder="留空使用默认 https://www.trae.cn"/><div class="hint">授权登录页。留空 = 默认。</div></div>
<div class="field"><label>Trae SOLO Agent 网关</label><input v-model="hostOverrides.traesolo.agent_host" placeholder="留空使用默认 https://trae-api-cn.mchost.guru"/><div class="hint">chat / models。留空 = 默认。</div></div>
<!-- TraeWork -->
<div class="field"><label>TraeWork Agent 网关</label><input v-model="hostOverrides.traework.agent_host" placeholder="留空使用默认 https://trae-api-cn.mchost.guru"/><div class="hint">chat sessions。留空 = 默认。</div></div>
<div class="field"><label>TraeWork 积分地址</label><input v-model="hostOverrides.traework.ug_host" placeholder="留空使用默认 https://api.trae.cn"/><div class="hint">签到/积分。留空 = 默认。</div></div>
```

> 若字段太多导致"后端参数"卡片过长,可在卡片内用 `<details>` 折叠"高级平台覆盖",默认收起。由你判断,保持可用即可。

## 4. 测试

扩展 `tests/test_host_override.py`,新增:
```python
def test_phase_b_fields():
    from providers.host_override import CHANNEL_HOST_FIELDS
    assert CHANNEL_HOST_FIELDS["qclaw"] == ("jprx_gateway", "aizone_base")
    assert CHANNEL_HOST_FIELDS["traesolo"] == ("oauth_host", "console_host", "agent_host")
    assert CHANNEL_HOST_FIELDS["traework"] == ("agent_host", "ug_host")

def test_phase_b_override(monkeypatch):
    monkeypatch.setattr(db, "get_setting",
        lambda k, d=None: {"qclaw": {"aizone_base": "https://mirror.example.com/aizone/v1"}})
    from providers.host_override import channel_host
    assert channel_host("qclaw", "aizone_base", "https://default") == "https://mirror.example.com/aizone/v1"
    assert channel_host("qclaw", "jprx_gateway", "https://default") == "https://default"
    assert channel_host("traesolo", "agent_host", "https://default") == "https://default"
```

运行:`pytest -q tests/test_host_override.py tests/test_core.py tests/test_web_assets.py`

## 5. 交付后自检

- [ ] `pytest -q tests/test_host_override.py` → 全 passed(Phase A 3 个 + Phase B 新增)
- [ ] `pytest -q tests/test_core.py` → 无新失败(1 个 pre-existing 忽略)
- [ ] `pytest -q tests/test_web_assets.py` → 14 passed
- [ ] `git grep -n "—" -- web/ providers/` → 0(中文零 em-dash)
- [ ] 只 add 明确文件

## 6. Commit 纪律(可拆 2 颗)

```
1. feat(providers): per-channel host override for qclaw/traesolo/traework
   → add: providers/host_override.py, providers/qclaw/jprx.py,
          providers/qclaw/chat.py, providers/traesolo/token.py,
          providers/traesolo/chat.py, providers/traesolo/login.py,
          providers/traesolo/store.py, providers/traework/chat.py,
          providers/traework/quota.py, providers/traework/token.py,
          providers/traework/store.py, tests/test_host_override.py
2. feat(web): per-channel host override UI for qclaw/traesolo/traework
   → add: web/js/pages/settings.js
```

每颗:`git commit -m "..."` → `git push origin refactor/web-console-ia` → 在 `redesign-audit/05-implementation-log.md` 追加 Phase B 段。

## 7. 兜底

- 若某处引用点与 spec 行号不符:按**符号名**(`JPRX_GATEWAY`/`AIZONE_BASE`/`OAUTH_HOST`/`CONSOLE_HOST`/`AGENT_HOST`/`AGENT_API`/`UG_API`)全局 grep 定位。
- 若某文件已 import `channel_host`(Phase A 可能已加),不要重复 import。
- 若 `channel_host()` 在无 account 参数的函数里用,直接 `channel_host(CHANNEL_ID, field, DEFAULT)` 即可。
- 若 pytest 出现与本次改动**无关**的新失败:停下报告,不要乱修。
- 中文 UI 文案零 em-dash;英文 commit/注释可含。
