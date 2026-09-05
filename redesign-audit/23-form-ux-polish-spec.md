# 浮窗表单易用性优化 Spec

- 分支:`feature/bailian-provider`(延续)
- 状态:待实现
- 用户需求:
  1. 默认扫描目录显示不全 → 悬停显示完整路径(悬停已可用的加确认);
  2. 模型白名单、别名改**非必填**,白名单留空默认 `["DeepSeek-V4-Flash"]`;
  3. 环境变量名留空时**默认拼接** `CB_<通道ID大写>`;
  4. 必填项 `*` 用红色,前端全页面排查。

## 1. 后端(custom_channels.py + admin.py)——规格变更点

`validate_definition()`(src/providers/custom_channels.py L56-136)放宽:

- `models`:非空数组 → **可选**。缺省/空/None 时取默认 `["DeepSeek-V4-Flash"]`(常量 `DEFAULT_MODELS = ("DeepSeek-V4-Flash",)` 定义在模块顶部);
- `aliases`:本就可选(None → {}),语义不变;
- `env_api_key`:**留空自动生成**——校验通过后若为空,`env_api_key = "CB_" + cid.upper()`(slug 字符集 `[a-z0-9_-]` 大写后合法,`-`/`_` 保留);显式传值仍按 `^CB_[A-Z0-9_]+$` 校验;**返回生成的值**让前端可提示。

注意:`validate_definition` 不修改原 dict(纯校验),自动生成逻辑放 **admin.py 的 POST/PUT handler**(在 validate 之后、upsert 之前):`if not data.get("env_api_key"): data["env_api_key"] = "CB_" + cid.upper()`;models 缺省同理在 handler 补默认值。这样 `validate_definition` 保持纯函数,测试也好写。

**测试**(tests/test_custom_channels.py):
- validate_definition:models 缺省不再报错(纯校验层面);env 留空不再报错;
- 新增 handler 级测试:POST /admin/channels/custom 不带 models/env_api_key → 定义里 models==["DeepSeek-V4-Flash"]、env_api_key=="CB_<ID>";别名指向模型校验仍生效(指向默认模型)。

## 2. 前端(channels.js 浮窗表单 + 全局 `*` 红色)

### 2.1 浮窗密钥型表单
- 移除「模型白名单」的 `*` 与必填校验(`umSave` 里"至少填一个模型 id"删除),hint 改"可选;留空默认 DeepSeek-V4-Flash,保存后可在「模型配置」页调整或用探活拉取";
- 「别名」已是可选,hint 不变;
- 「环境变量名」hint 改"可选;留空自动生成 CB_<通道ID大写>";
- `umSave`:models 空数组时不传 body.models(让后端补默认),env 留空不传(让后端生成);保存成功后若后端返回的 definition 带生成的 env,toast 提示生成的变量名(可选,简化为不提示也可,报告说明选择)。

### 2.2 必填 `*` 红色(全页面排查)
- app.css 加一条:`.field label .req{color:var(--err);font-weight:700}`(或直接 `label .req`);
- channels.js 浮窗:必填项的 `*` 包 `<span class="req">*</span>`(id/显示名/Base URL/API Key——**API Key 在 create 必填、edit 可空**:edit 模式用 JS 条件渲染 `*`,不做静态红色误导);
- **全页面排查**其他 pages(keys.js/settings.js/models.js/accounts 逻辑已并入 channels 等)所有 `label` 里的 `*`,统一替换为 `<span class="req">*</span>`;没有 `*` 的表单不新增;
- 旧语义核查:凡后端实际可选但标了 `*` 的(如密钥型编辑的 API Key),去掉 `*`;必填但没标的补上(排查后按实际后端契约定)。

## 3. 扫描目录悬停(_login_import.js)

`.detect-path code` 已有 `:title="d.path"`(悬停原生 tooltip)——**确认该项已满足**,但目录显示不全的根因是 CSS `text-overflow:ellipsis`。增强:
- 保持 ellipsis + `:title`(已有);
- `.detect-path` 加 `word-break:break-all` 备选方案不采用(会换行破坏布局);
- 检查 `detect-box` 宽度过窄的根因:`.detect-grid` 两列布局中列宽受限,给 `.detect-path code` 的 `:title` 确认存在即可;**额外**给 `.detect-path` 加 `title` 冒泡(hover 行任意位置都能看到完整路径),并把 badge 也包进 title;
- 其它 pages 若有同样 ellipsis 路径展示(usage/logs 里 provider/model 列),确认已有 `:title` 则不动,没有的补。

## 4. 校验与验收

- 三件套:node --check(channels/_login_import);check_roots2 全页面 OK;pytest tests/test_custom_channels.py(含新断言) tests/test_custom_channels_gmi.py tests/test_custom_channels_bailian.py tests/test_web_assets.py tests/test_docs_encoding.py -q -p no:cacheprovider($env:PYTHONPATH="$PWD\src");
- 验收清单:
  - [ ] 浮窗不填模型白名单/别名/env → 保存成功,通道详情显示默认模型与生成的 env 名;
  - [ ] 浮窗必填项 `*` 红色,可选项无 `*`;
  - [ ] 其它页面表单的 `*` 同样红色;
  - [ ] 悬停扫描目录行任意处可见完整路径;
  - [ ] 全量回归通过。

## 5. Out of Scope
- 探活成功后回填模型列表(既有遗留,不变);
- 别名指向校验规则不变(仍要求指向 models 内的 id——注意留空 models 用默认值时,别名值须指向 DeepSeek-V4-Flash 或用户填的模型)。
