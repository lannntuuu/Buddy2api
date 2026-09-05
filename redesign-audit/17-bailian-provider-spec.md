# 阿里百炼（Bailian）渠道接入 Spec

- 分支:`feature/bailian-provider`
- 状态:待实现
- 关联需求:新增阿里百炼平台,请求地址 `https://llm-7dqe434wikmhz0wa.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`

## 1. 背景与结论

Buddy2api 的渠道适配层已经有一个完全同构的先例:**GMI Cloud**(`src/providers/gmi/`,v2.2 引入的 opt-in OpenAI 兼容渠道)。阿里百炼的 OpenAI 兼容模式(MaaS 专属实例地址,`/compatible-mode/v1`)与 GMI 的接入形态完全一致:

- OpenAI Chat Completions 协议,`Authorization: Bearer <API Key>`;
- 单 API Key 账号,无 refresh、无本机登录目录,凭据从管理页粘贴或环境变量导入;
- 无签到、无公开余额端点(与 GMI 相同,quota 走 unsupported 快照);
- `/models` 动态拉模型列表。

因此本 spec 的实现策略是**按 GMI 模板复制一个新渠道包**,不做进一步抽象(gmi/chat.py 顶部的 ponytail 注释明确要求:新增 OpenAI 兼容平台靠复制文件,而不是继续泛化)。

渠道标识:`bailian`。

## 2. 上游事实

| 项 | 值 |
|---|---|
| Base URL | `https://llm-7dqe434wikmhz0wa.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`(MaaS 专属实例,管理页/环境变量可覆盖) |
| Chat | `POST {base}/chat/completions` |
| Models | `GET {base}/models`(Bearer 鉴权) |
| 鉴权 | `Authorization: Bearer <DASHSCOPE_API_KEY>` |
| 流式 | SSE,`stream_options.include_usage=true` 时末帧带 usage(OpenAI 兼容) |
| 模型 | 专属实例上已部署的模型(如 qwen 系列、deepseek 系列),以 `/models` 实际返回为准 |
| 已知差异 | 百炼兼容模式对 `enable_thinking`、`incremental_output` 等私有参数敏感;本渠道不做任何请求改写,客户端传什么就透传什么(与 GMI 一致) |

静态兜底模型表:未知专属实例上部署了什么模型,所以 `STATIC_MODELS` 只放占位 `qwen-plus`(Admin 可在"渠道与模型"里改白名单,动态 `/models` 会覆盖它)。不存在"猜模型"的需求。

## 3. 新增文件:`src/providers/bailian/`

### 3.1 `constants.py`

仿 `providers/gmi/constants.py`:

- `CHANNEL_ID = "bailian"`,`DISPLAY_NAME = "阿里百炼 Bailian"`;
- `DEFAULT_BASE_URL = "https://llm-7dqe434wikmhz0wa.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"`;
- `EP_MODELS = "/models"`,`EP_CHAT = "/chat/completions"`;
- `STATIC_MODELS = ("qwen-plus",)`,`DEFAULT_MODEL = STATIC_MODELS[0]`;
- `ALIASES`:`auto`/`bailian`/`bailian/auto` → `DEFAULT_MODEL`;
- `MODELS_CACHE_TTL = 600.0`,`SINGLE_ACCOUNT = True`;
- `ENV_API_KEY = "CB_BAILIAN_API_KEY"`,`ENV_AUTH_DIR = "CB_BAILIAN_AUTH_DIR"`(auth_dir 仅占位,百炼无本机目录扫描)。

### 3.2 `store.py`

仿 `providers/gmi/store.py`,把 `gmi` 字样换成 `bailian`:

- `_normalize_key`:支持裸 Key / `Bearer xxx` / `{"api_key": ...}` 三种粘贴形态;
- `parse_credentials`:uid 为 `bailian-{key[-8:]}`,`domain` 存 base_url,`extra.base_url` + `extra.source`;
- `discover()`:空 stub(无 IDE 目录);
- `import_path()`:txt/json 文件导入;
- `upsert_account()`:单 key 平台,同一时刻仅一行 active,其余置 inactive;返回 `{"id", "updated", "row"}` 契约(回归 tests/test_gmi_store.py 踩过的 500 坑);
- `ensure_env_account()`:`CB_BAILIAN_API_KEY` 幂等 bootstrap。

### 3.3 `chat.py`

整体复制 `providers/gmi/chat.py`,改动仅限:

- import 路径、logger 名 `bailian.chat`;
- `User-Agent: buddy2api/bailian`;
- 503 文案改为提示 `CB_BAILIAN_API_KEY` / 管理页导入;
- 其余(动态模型缓存、SSE 透传、usage 记账 `_record`、test_chat)**逐行为等价**,不做协议改写。

### 3.4 `quota.py`

仿 gmi/quota.py:`fetch_quota` 返回 `QuotaSnapshot(unsupported=True)`,message 说明百炼兼容模式无余额端点;`fetch_checkin`/`claim_checkin` 返回 `enable: False`。

### 3.5 `__init__.py`

仿 `providers/gmi/__init__.py`:`BailianProvider`,实现 `Provider` 协议全部方法(list_models / fetch_model_rates / refresh_dynamic_models / alias_map / accepts_model / translate_model / pick_account / pick_account_with_fallback / has_usable_account / chat_completions / parse_credentials / discover / import_path / upsert_account / fetch_quota / fetch_checkin / claim_checkin / test_chat / refresh→noop),末尾 `PROVIDER = BailianProvider()`。

## 4. 注册触点(全量清单)

| 文件 | 改动 |
|---|---|
| `src/providers/protocol.py` | `ChannelId` Literal 与 `KNOWN_CHANNEL_IDS` 追加 `"bailian"` |
| `src/providers/__init__.py` | import `BAILIAN_PROVIDER`;`_LOADED` 加 `"bailian"`;**不进** `DEFAULT_PROVIDER_IDS`;`OPT_IN_PROVIDER_IDS` 追加 `"bailian"`(与 gmi 同为 opt-in) |
| `src/accounts/control_plane.py` | import bailian 的 `STATIC_MODELS`/`ALIASES`;`_CHANNEL_DEFAULTS` 加 `"bailian"` 条目 |
| `src/providers/host_override.py` | `CHANNEL_HOST_FIELDS` 加 `"bailian": ("base_url",)` |
| `src/web/js/pages/accounts.js` | `channels` 下拉加 `{id:'bailian',display_name:'阿里百炼'}`;`APIKEY_CHANNELS` 加 `bailian` 条目(name/base/env),复用现有 gmi API-Key 导入面板逻辑 |
| `src/web/js/pages/settings.js` | `hostOverrides` 加 `bailian:{base_url:''}`;load/save/channel_hosts 提交与输入框补 bailian(placeholder 提示默认地址) |
| `src/web/js/pages/channels.js` | `canRefreshOfficial` 条件加 `c.channel==='bailian'`(动态 /models 刷新与 gmi 同权) |
| `README.md` / `README_EN.md` | 渠道表加 Bailian 行(opt-in,`CB_GATEWAY_PROVIDERS` 尾部追加启用);环境变量表加 `CB_BAILIAN_API_KEY` |
| `config.example.toml` | 如有 providers 示例行,补 `bailian` 注释 |

无需动的部分:数据库 schema(accounts 表 provider 为自由字符串)、`gateway/routers/*`(渠道注册表驱动)、`upstream/*`。

## 5. 启用方式

- 环境变量:`CB_GATEWAY_PROVIDERS=workbuddy,qclaw,qwenwork,traework,traesolo,bailian`;
- 或管理页"设置 → 渠道开关"勾选(默认关闭,opt-in 与 GMI 一致)。

## 6. 测试(`tests/test_bailian_store.py`)

仿 `tests/test_gmi_store.py`,覆盖:

1. `parse_credentials` 三种粘贴形态(裸 Key / Bearer / JSON 包裹)+ 空 Key 报错 + 自定义 base_url;
2. `upsert_account` 插入/更新契约 + 同 Key 幂等 + 单 active 行约束;
3. `ensure_env_account` 三态(env 未设 / env 设置且无活跃账号 / 已有活跃账号);
4. `providers.get_provider("bailian")` 在 CB_GATEWAY_PROVIDERS 含 bailian 时可用;
5. `control_plane.channel_model_view("bailian")` 与 `set_channel_models` 持久化;
6. 白名单拦截:未在 `bailian.models` 内的模型 400;
7. `test_provider_schema.py` 风格的 enabled/known 通道断言补 bailian。

验收命令:`.venv` 下 `python -m pytest tests/test_bailian_store.py tests/test_gmi_store.py tests/test_provider_schema.py tests/test_host_override.py -x -q`(既有 gmi 测试必须保持全绿,证明无回归)。

## 7. 明确不做(Out of Scope)

- 不做百炼私有 SDK / DashScope 原生协议(仅 OpenAI 兼容模式);
- 不做余额查询(无公开端点)、不做签到;
- 不改写请求体(透传;`enable_thinking` 等由客户端自行传);
- 不做泛化"OpenAI-compat 多租户表"——按 gmi 的 ponytail 原则,下一个平台继续复制文件。
