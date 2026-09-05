# 浮窗表单微调第二批:字段互换 + env 动态预填 Spec

- 分支:`feature/bailian-provider`(延续)
- 状态:待实现
- 用户需求:
  1. **API Key 与 Base URL 互换位置**——新顺序:通道 ID → 显示名称 → **Base URL** → **API Key** → 模型白名单 → 别名 → 环境变量名;
  2. **通道 ID 修改时,环境变量名动态跟随**——create 模式下 env 输入框为空时,实时预填 `CB_<通道ID大写>`(只动前端显示与提交预填;后端生成逻辑保留作为兜底)。

## 1. 改动范围(仅 channels.js;后端零改动)

### 1.1 字段互换
- form tab 模板中 API Key 与 Base URL 两个 `<div class="field">` 块整体交换位置;其余字段与逻辑不动。

### 1.2 env 动态跟随
- 现状:env 输入框 `v-model="um.draft.env_api_key"`,留空靠后端生成;用户不知道会生成什么名,体验差;
- 目标行为(create 模式,`um.mode==='create'`):
  - `watch(() => um.value.draft.id, ...)`:当 env_api_key **仍为空或等于上一个自动值**时,把 env_api_key 实时更新为 `'CB_' + id.trim().toUpperCase()`;
  - **用户手动改过 env 则不再覆盖**(跟踪 `um.envTouched`:env input `@input` 置 true;umClose/openKeyModal 重置);
  - slug 字符集 `[a-z0-9_-]` 大写后均合法(`-`/`_` 保留),无需过滤;id 为空时预填清空;
  - edit 模式**不**跟随(edit 的 env 属于已存在定义,留空=由后端按新 id 重新生成的语义仅在用户显式清空时发生,不做实时预填干扰);
- 提交路径不变:`umSave` 空 env 仍不传、由后端生成兜底(两端结果一致)。

## 2. 校验与验收
- 三件套:node --check channels.js;check_roots2 全页面 OK;pytest tests/test_custom_channels.py tests/test_custom_channels_gmi.py tests/test_custom_channels_bailian.py tests/test_web_assets.py tests/test_docs_encoding.py -q -p no:cacheprovider($env:PYTHONPATH="$PWD\src");
- 验收:
  - [ ] form tab 顺序为 通道 ID → 显示名称 → Base URL → API Key → 模型白名单 → 别名 → 环境变量名;
  - [ ] create 模式输入通道 id(如 `siliconflow`)→ env 框实时显示 `CB_SILICONFLOW`;
  - [ ] 手动改过 env 后再改 id,env 不被覆盖;
  - [ ] 清空手动值后改 id,重新跟随;
  - [ ] edit 模式 env 不实时预填;
  - [ ] 保存链路与之前一致(空 env 仍由后端兜底生成);
  - [ ] 回归通过。

## 3. Out of Scope
- 后端零改动;登录型向导/info tab 不动;其它字段顺序不动。
