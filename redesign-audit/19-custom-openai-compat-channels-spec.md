# 自定义 OpenAI 兼容渠道(数据驱动)Spec

- 分支:`feature/bailian-provider`(延续)
- 状态:待实现
- 前置决策(用户逐项拍板):
  - **D1** Key 入 accounts 表,渠道定义只存描述;
  - **D2** 渠道 ID 用 slug 白名单校验;
  - **D3** 保存后缓存失效、下次请求重建;
  - **D4** 前端全面动态化;
  - **D5** gmi/bailian 一并数据化(seed 迁移),**seed 后删除两个代码包**;
  - **D6** 删除渠道时账号行置 inactive;
  - **D7** base_url 必须 https(放行本机 http)+ 保存时 `GET /models` 探活;
  - **D8** 定义里的 models 仅作首次兜底,后续由 `<channel>.models` settings 机制接管。

## 1. 目标

"一个 URL + 一个 Key + 模型 ID" 形态的平台(百炼、GMI 及未来同类)收敛为一类:**管理员在管理页表单里新增/编辑/删除,零代码、热生效**。现有 gmi/bailian 转为数据定义,协议实现收敛到单一基类。

## 2. 架构

```
src/providers/openai_compat.py          # 基类(单文件):协议实现 + store 逻辑
├── OpenAICompatProvider                # 从"定义 dict + 渠道 id"构造
│   ├── chat_completions / _stream_chat / _run_non_stream / _record / test_chat
│   ├── parse_credentials / upsert_account / ensure_env_account / discover
│   ├── list_models / refresh_model_ids / alias_map / accepts_model / translate_model
│   └── fetch_quota(unsupported) / fetch_checkin / claim_checkin
└── CustomOpenAICompatProvider(OpenAICompatProvider)  # 定义来自 custom_channels

src/providers/gmi/ bailian/             # 删除(seed 迁移后)
```

基类从现 gmi 包的 chat/store/quota 提取,行为逐行等价;渠道差异全部来自定义 dict:

```json
{
  "id": "bailian",
  "display_name": "阿里百炼",
  "base_url": "https://llm-....maas.aliyuncs.com/compatible-mode/v1",
  "models": ["qwen-plus"],
  "aliases": {"auto": "qwen-plus"},
  "env_api_key": "CB_BAILIAN_API_KEY",
  "source": "seed",
  "created_at": 1700000000
}
```

## 3. 存储与注册层

### 3.1 settings 键 `custom_channels`

- JSON 数组,存定义(无 Key);Key 仍走 accounts 表(provider=<渠道 id>);
- 读写经 `providers/custom_channels.py` 新模块:`list_definitions() / save_definitions() / get_definition(id)`,内部走 `db.get_setting/set_setting`;
- 校验函数 `validate_definition()`:
  - `id` 匹配 `^[a-z][a-z0-9_-]{0,31}$`,不得与内置 KNOWN_CHANNEL_IDS 及其它自定义 id 重复;
  - `base_url` 必须 `https://`,或 `http://127.0.0.1[:port]` / `http://localhost[:port]`(本机调试);
  - `display_name` 非空 ≤ 40 字;`models` 非空字符串数组;`aliases` 为 `别名->id` 对象且值须在 models 内;
  - `env_api_key` 可选,匹配 `^CB_[A-Z0-9_]+$`。

### 3.2 `providers/__init__.py` 改造

- 自定义渠道实例缓存:`_custom_cache: dict[str, Provider]`,由 definitions 构建;
- `known_channel_ids()` 新函数:内置 + 自定义;`is_known_channel`、`_parse_enabled` 的过滤、`admin_channels` 的列表全部改用它(运行时无 Literal 硬依赖,`protocol.ChannelId` 保留为内置文档性标注);
- `get_provider`:内置查 `_LOADED`,否则查自定义缓存;
- 失效:`custom_channels.py.invalidate_cache()` 清空 `_custom_cache`(D3);
- `OPT_IN_PROVIDER_IDS` 保留语义:seed 渠道默认不在 `DEFAULT_PROVIDER_IDS`,启用仍走 enabled_channels / env;
- `register_provider` 不变(测试用)。

### 3.3 模型默认与 `_CHANNEL_DEFAULTS`(D8)

- `control_plane._CHANNEL_DEFAULTS` 中 gmi/bailian 条目删除;自定义渠道的"内置默认"从定义的 `models`/`aliases` 取:
  `control_plane` 增加分支——渠道是自定义时,默认值来自 `custom_channels.get_definition(id)`,且**当定义被编辑(模型列表变更)而用户从未写过 `<id>.models` 时,兜底跟随新定义**;
- `<channel>.models` / `<channel>.aliases` settings 机制不变,自定义渠道直接兼容(它按字符串 id 键控)。

## 4. admin API(CRUD + 探活)

`gateway/routers/admin.py` 新增:

| 端点 | 行为 |
|---|---|
| `GET /admin/channels/custom` | 列出定义;**Key 永不回显**(无 Key 字段) |
| `POST /admin/channels/custom` | 创建。body: `{id, display_name, base_url, models?, aliases?, api_key, env_api_key?}`;校验(D2/D7)后:若带 `api_key`,立即用它 `GET {base}/models` 探活 → 失败返回 `{"ok": false, "probe_status": N, ...}` 且 **HTTP 200 + warning**(允许保存,D7);探活成功时把返回的模型 id 列表回填为首次白名单(若 body 未给 models);api_key 有值时写入 accounts 表(复用基类 parse/upsert,uid=`<id>-{key[-8:]}`);保存后 invalidate |
| `PUT /admin/channels/custom/{id}` | 更新 display_name/base_url/models/aliases;`api_key` 可选(有值则轮换账号行 Key);base_url 变更后探活同上;invalidate |
| `DELETE /admin/channels/custom/{id}` | 移除定义 + 该 provider accounts 行全部置 inactive(保留日志,D6);`source=='seed'` 的渠道**不允许删除**(只能停用),返回 409;invalidate |
| `GET /admin/channels/{channel}/models` 及 refresh 端点 | 自定义渠道自动兼容(经 known_channel_ids/get_provider),无需新代码,验证即可 |

`GET /admin/channels`(现有)扩展:每项加 `"kind": "builtin" | "apikey"` 与 `"custom": bool`——这是前端动态化的数据源(D4)。

## 5. Seed 迁移(D5)与旧包删除

启动时(`gateway/server.py` lifespan 或 providers 首次解析,选 lifespan):

1. `custom_channels` settings 键**不存在**(从未写过,区别于空数组)时:写入两个 seed 定义(gmi、bailian,值取自现 constants.py;若 `channel_hosts` 里存有 `gmi.base_url` / `bailian.base_url` 覆盖,合并进 seed 的 base_url 后清除该覆盖项);
2. 账号行、`enabled_channels`、`<id>.models`/`<id>.aliases` **全部不动**(id 未变,天然兼容);
3. `CB_GMI_API_KEY` / `CB_BAILIAN_API_KEY` 环境变量:由定义的 `env_api_key` 字段承接,基类 `ensure_env_account` 行为不变;
4. 迁移完成后删除 `src/providers/gmi/`、`src/providers/bailian/` 两个包;清理引用:
   - `providers/__init__.py` 移除两包 import 与 `_LOADED` 条目;
   - `control_plane.py` 移除 `_GMI_DEFAULT_*` / `_BAILIAN_DEFAULT_*` import 与 `_CHANNEL_DEFAULTS` 条目;
   - `host_override.py` 移除 gmi/bailian 条目(自定义渠道 base_url 走定义);
   - README 双语的项目结构树与 v2.2 更新内容中的 gmi/bailian 表述改为"内置 seed 渠道(可编辑)"口径。
5. `OPT_IN_PROVIDER_IDS` 语义迁移:seed 定义不自动启用,`DEFAULT_PROVIDER_IDS` 不含 gmi/bailian(与现状一致);env `CB_GATEWAY_PROVIDERS` 里出现 seed id 照常生效(经 known 集合过滤)。

> 显式不做:自定义渠道不进 hostOverrides 设置页(改 base_url 走渠道编辑表单);`security.md`/`SECURITY.md` 不动。

## 6. 前端全面动态化(D4)

数据源:`GET /admin/channels`(扩展后的 kind/custom 字段)。

| 文件 | 改动 |
|---|---|
| `pages/channels.js` | 渠道列表已动态,加"自定义渠道"管理区:定义列表 + 新增/编辑表单(id、名称、Base URL、API Key 粘贴、模型逗号分隔、可选 env 名)+ 删除按钮(seed 渠道删除按钮禁用)+ 探活结果提示;`canRefreshOfficial` 改为 `c.kind==='apikey'`(去硬编码 id) |
| `pages/accounts.js` | 下拉改为吃 `/admin/channels` 数据;`APIKEY_CHANNELS` 删除硬编码,面板对 `kind==='apikey'` 渠道通用(显示该渠道 base_url placeholder 与 env 提示——数据从定义来,经 channels 接口透出);变量名 `gmiMode` 等可顺手更名 `keyMode` |
| `pages/keys.js` | 若渠道下拉硬编码,同样改为数据驱动(实现时确认) |
| `pages/usage.js` / `pages/logs.js` | 渠道过滤下拉改数据驱动(实现时确认,当前若有硬编码 id 列表则同改) |
| `pages/settings.js` | hostOverrides 移除 gmi/bailian 两块(seed 后 host_override 不再管辖);其余不动 |
| `test_web_assets.py` | 若有渠道相关断言,同步 |

前端仍是无构建压缩风格,改动贴合现有单行写法。

## 7. 测试

- **迁移**:`tests/test_gmi_store.py`、`tests/test_bailian_store.py` 改造为针对 seed 后自定义渠道的等价测试(fixture:seed 定义 + isolated_db),改名 `test_custom_channels_gmi.py` / `test_custom_channels_bailian.py` 或合并;parse/upsert/env 三态/单 active/白名单闸门断言全部保留(测试即规格,行为不得漂移);
- **新增** `tests/test_custom_channels.py`:
  - validate_definition 全规则(合法 slug、重复 id 拒绝、http 限本机、aliases 指向不存在的 model 拒绝、env 名格式);
  - CRUD API 契约(创建→get_provider 可用→探活 warning 路径→PUT 轮换 key→DELETE 置 inactive→seed 删除 409);
  - 缓存失效:修改定义后 get_provider 反映新值,无需重启;
  - `_parse_enabled`/`known_channel_ids` 含自定义渠道;env 锁定模式下行为;
  - seed 迁移幂等:第二次启动不重复写;channel_hosts 覆盖合并进 seed;
  - `<id>.models` settings 接管:定义兜底 vs 用户白名单优先级。
- **回归**:`tests/test_provider_schema.py`(known/opt-in 断言按新集合调整)、`test_host_override.py`(移除 gmi/bailian 用例)、`test_docs_encoding.py`、`test_web_assets.py` 全绿。

## 8. 风险与边界

- **升级安全**:id 不变 → accounts/settings/enabled 全兼容;唯一破坏点是 import 路径(`from providers.gmi import ...` 的外部脚本会挂),仓库内部无此用法(grep 验证);
- **探活超时**:10s 上限,失败不阻塞保存;
- **并发写**:settings 表 KV 写入无版本控制,管理页单人使用场景可接受(与现有 set_channel_models 同级风险);
- **不做**:多 Key 轮换、余额查询、非 OpenAI 兼容协议、渠道级 prompt 清洗。

## 9. 验收清单

- [ ] 管理页可新增/编辑/停用/删除自定义渠道,全程零重启;
- [ ] 新增渠道后:账号页出现该渠道并可粘 Key;创建 API Key、`/v1/models`、`/v1/chat/completions`(流式+非流式)、日志、用量统计全链路可用;
- [ ] 旧库升级:gmi/bailian 账号、Key、白名单、启用状态无损;`src/providers/{gmi,bailian}/` 已删除;
- [ ] 63 项既有渠道测试以迁移后形态全绿 + 新增 custom_channels 测试全绿。
