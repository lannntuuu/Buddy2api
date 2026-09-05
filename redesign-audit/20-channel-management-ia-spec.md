# 信息架构重组:「通道管理」主从式 +「模型配置」独立页 Spec

- 分支:`feature/bailian-provider`(延续)
- 状态:待实现
- 用户拍板:① 页面名「通道管理」;② 主从式布局;③ 通道启用开关一并搬入;④ 路由键改彻底。

## 1. 目标

把上次概念讨论的成果落到导航结构:通道(定义+凭证+开关)住一个页面,模型住另一个页面,消灭"接一个通道跨三页"的动线。

## 2. 新信息架构

```
「通道管理」 路由键 channels(语义变更)
  主:通道列表(全部通道;启用开关;kind/custom 徽标;选中态)
  从(按所选通道):
    ├─ 定义区:自定义通道显示编辑表单(复用现有 CRUD);内置通道显示只读摘要
    ├─ 凭证区:登录型 → 本机检测/自定义路径/网页登录(现 accounts.js 逻辑)
    │          密钥型 → 上游密钥粘贴面板(KEY_PANEL 语义)
    └─ 凭证列表:现 accounts.js 的账号/密钥表(按通道过滤或全量,保留筛选)

「模型配置」 路由键 models(新)
  ├─ 统一模型(原样迁入)
  └─ 各通道白名单/别名/倍率/思考档位(原样迁入)

「设置」:通道开关面板移除(已搬到通道管理);hostOverrides 已清,其余不动
```

接新通道动线(目标):通道管理一页内 建定义 → 开开关 → 贴密钥,完成。

## 3. 路由与导航(`app.js`)

- `validPages`:`['dashboard','channels','models','keys','usage','logs','quota','setup','settings']`——`accounts` 删除;
- nav 数组:改名「通道管理」(key `channels`,icon 沿用合适的)、「模型配置」(key `models`),删除「账号管理」;顺序建议:dashboard / channels / models / keys / usage / logs / quota / setup / settings;
- **localStorage 迁移**:`cb_gw_page` 读取时映射 `accounts → channels`、legacy `channels → models`(老用户记忆的"模型页"落到新模型页);映射后立即回写新值;
- 页面标题/副标题(phead)同步:「通道管理 · 定义通道 · 管理凭证 · 启用开关」「模型配置 · 统一模型 · 白名单与别名」。

## 4. `pages/channels.js` 重写(主从式)

文件级重写,内容三来源:原 channels.js 的自定义通道 CRUD + 原通道开关逻辑 + 原 accounts.js 全部凭证逻辑。

### 4.1 主列表(左/上)

- 数据:`GET /admin/channels`(含 kind/custom/enabled/env_locked)+ `GET /admin/channels/custom`;
- 每行:显示名、id(mono)、徽标(`kind: builtin/apikey`、`custom/seed`)、enabled 开关(Switch 样式);
- 开关:`PUT /admin/channels {enabled, order}`;env_locked 时禁用并提示 CB_GATEWAY_PROVIDERS 锁定(沿用现 settings 面板逻辑);
- 选中态管理:`activeChannel=ref('')`,默认选第一个;切换即刷新详情。

### 4.2 详情区(右/下)

- **定义区**:custom → 内嵌编辑表单(现 ccForm 逻辑,术语已是「通道」);seed → 只读摘要 + 「编辑」入口(seed 可编辑可停用不可删除);builtin 登录型 → 只读摘要(登录目录路径等,取自现有文案);
- **凭证区**(按 kind 分支,从 accounts.js 搬入并挂到选中通道):
  - `kind==='apikey'`:KEY_PANEL 粘贴面板(上游密钥语义,已按上次交付措辞);
  - 登录型:本机检测(discover/scan/authPath)、自定义路径、Trae SOLO 网页登录(solo 状态机)——全部以**当前选中通道**为上下文(替换原 discChannel);
- **凭证列表**:账号/密钥表(现 accounts.js 的 visibleAccounts/筛选/操作列),全局展示(不再按选中通道强过滤,保留通道列;或加"仅看当前通道"快捷筛选——实现取简单者,报告说明);
- 「高级手动添加」弹窗保留。

### 4.3 原通道与模型页的内容

统一模型 + 各平台设置两块卡片**整体迁出**到 models.js(channels.js 不再保留)。

## 5. `pages/models.js`(新建)

- 从 channels.js 迁入:统一模型卡片、各平台设置卡片、loadAll 中相关拉取(/admin/unified-models、/admin/channels/{id}/models);
- 通道下拉数据源不变(GET /admin/channels);刷新官方模型表按钮随迁;
- 命名:导出默认对象,结构与现 pages 一致。

## 6. `pages/settings.js` 瘦身

- 移除「通道开关」面板(details 区:enabled/order 勾选与保存)及其 state/submit 字段;
- 移除指向已搬走功能的 hint(若有);其余(后端 URL、超时、hostOverrides 高级区)不动。

## 7. 删除与兼容

- `pages/accounts.js` 删除(逻辑并入 channels.js);
- `app.js` import 改为 `models.js` + 重写后的 `channels.js`;
- 后端 API **零改动**(纯前端重组);
- README 双语:所有「账号页 / Accounts page」措辞 →「通道管理 / Channels page」,「通道与模型 / Channels & Models」→「模型配置 / Models」;涉及页面名的操作指引逐条核对修改;test_docs_encoding 守卫编码。

## 8. 测试与验收

- `tests/test_web_assets.py`(ESM 解析全部 pages + vendor 守卫)必须全绿——它天然覆盖新 models.js 与重写的 channels.js;
- 全量回归:docs_encoding + web_assets + custom_channels 三件套(后端没动,应无影响);
- 验收清单:
  - [ ] 侧栏无「账号管理」,有「通道管理」「模型配置」;
  - [ ] 老用户 saved page `accounts` 打开落在通道管理,`channels`(legacy)落在模型配置;
  - [ ] 通道管理内:建自定义通道 → 开关启用 → 贴密钥 → 测试连通,全程不离开该页;
  - [ ] env 锁定下开关只读;
  - [ ] 登录型通道:检测/导入/SOLO 网页登录在新页可用;密钥型:粘贴/测试可用;
  - [ ] 账号表筛选、权重/优先级编辑、测试/刷新/删除全部可用;
  - [ ] settings 页无通道开关;模型配置页功能与迁移前等价。

## 9. Out of Scope

- 后端 admin API 不动(含 /admin/channels 的 PUT 契约);
- dashboard/quota/usage/logs/setup 四页不动(setup 的文案属 README 类,若出现"账号页"字样一并改);
- 不引入构建工具/组件框架,保持压缩单行风格与现有 CSS 令牌。

## 10. 实现顺序

1. models.js 迁出 + app.js 路由(先保证模型页独立可用);
2. channels.js 重写(先骨架:主列表+开关,再凭证区合并,再账号表合并);
3. settings.js 瘦身 + accounts.js 删除;
4. localStorage 迁移 + README 措辞;
5. 测试回归 + 验收清单自检。
