# 浮窗表单微调:API Key 前置 + 别名行编辑器 Spec

- 分支:`feature/bailian-provider`(延续)
- 状态:待实现
- 用户需求:
  1. 浮窗表单中 **API Key 移到「显示名称」下面**(Base URL 之前)——密钥型通道的核心凭据前置;
  2. 「别名」从 textarea(`别名→模型id` 每行手打)换成**可增减行的列表编辑器**(类似 channels 页别名行的现有交互)。

## 1. 改动范围(仅 channels.js 浮窗表单;后端零改动)

### 1.1 字段顺序(create/edit 通用,form tab)
新顺序:
1. 通道 ID(edit 禁改)
2. 显示名称 *
3. **API Key**(create 红星必填;edit 留空=不轮换——现有逻辑不变,只挪位置)
4. Base URL *
5. 模型白名单(可选)
6. **别名(新行编辑器)**
7. 环境变量名(可选,自动生成)

### 1.2 别名行编辑器
- 数据形态:`um.draft.aliasRows = [{k:'', v:''}]`(数组替代原 `aliases` 字符串);
- UI:每行两个输入框(`别名` / `模型 ID`)+ 行尾「删除」按钮;底部「+ 添加别名」按钮;至少保留一行空行供填写(删除到最后一行时允许,提交时过滤空行);
- 提交转换:`umSave` 里 `aliasRows` → `aliases` 对象(过滤 k/v 空白,拼 `→` 冲突场景:重复 k 后者覆盖即可,与原 textarea 行为一致);
- 回填转换:编辑模式 `openKeyModal(def)` 时 `Object.entries(aliases)` → `aliasRows`(无别名时给一行空行);
- `umClose`/`umEmptyDraft` 重置为 `[{k:'',v:''}]`;
- 模板复用 channels 页「各平台设置」里别名行的现有写法(`aliasRows` + `rmRow`/`addRow` 风格),保持视觉一致;`.tmp/check_roots2.py` 会校验新模板引用(um.draft.aliasRows 是对象属性访问,不会被根级检查拦)。

## 2. 校验与验收
- 三件套:node --check channels.js;check_roots2 全页面 OK;pytest tests/test_custom_channels.py tests/test_custom_channels_gmi.py tests/test_custom_channels_bailian.py tests/test_web_assets.py tests/test_docs_encoding.py -q -p no:cacheprovider($env:PYTHONPATH="$PWD\src");
- 验收:
  - [ ] 浮窗 API Key 在显示名称下方;
  - [ ] 别名为可增减行列表,添加/删除行为正确;
  - [ ] 编辑已有带别名的通道,别名行正确回填;保存后定义中别名不变;
  - [ ] 空行被过滤,不产生垃圾别名;
  - [ ] 回归通过。

## 3. Out of Scope
- 后端契约不变(aliases 仍是 dict);其它表单字段不动;登录型向导不动。
