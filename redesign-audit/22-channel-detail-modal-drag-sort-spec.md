# 通道管理页优化:详情浮窗化 + 拖拽排序 Spec

- 分支:`feature/bailian-provider`(延续)
- 状态:待实现
- 用户拍板:
  1. **详情卡撤除**,改为列表行内「详情」按钮 → 浮窗展示;
  2. 通道列表支持**拖拽排序**(SortableJS 已 vendor),密钥型组固定在登录型组之后,组内可拖;
  3. 其余模块的下拉遵守该排序;
  4. subagent 用 hy3。

## 1. 改动总览(仅 channels.js + 少量 README 措辞;后端零改动)

### 1.1 详情浮窗化
- 删除 Card B(详情 card)整块;
- 通道列表两组的每一行右侧(启用开关左边)加「详情」按钮(`@click.stop` 防止触发行选中):
  - 登录型行 → 浮窗显示该平台只读摘要(类型/导入方式说明/签到支持)+ 提示"凭证在下方凭证区或浮窗向导中导入";
  - 密钥型行 → 浮窗显示现 Card B 的只读摘要(自定义通道含显示名/Base URL/模型/别名/env/来源;内置 seed 显示 keyPanelMetaById 摘要)+ 操作按钮(编辑→切浮窗表单;删除/删除禁用);
  - 复用统一浮窗外壳:新增 um.tab 状态(`'info'` 为详情视图,`'form'` 为编辑表单);编辑按钮在详情浮窗内直接切到 form tab;
- onMounted 默认选中通道逻辑保留(activeChannel 仍驱动 Card C/浮窗详情)。

### 1.2 拖拽排序
- 两组各一个 `<tbody>`(或分组表格两个 table),`Sortable.create(el, {handle:'.drag-handle', onEnd})`,每行行首加 `.drag-handle`(≡ 图标,I.refresh 不合适则用文字「≡」或新增 icon);
- **组间固定**:登录型组在上、密钥型组在下(硬编码顺序,不参与跨组拖拽);组内拖拽只重排本组;
- 拖完保存:把两组顺序拼接为完整 order 数组(登录组在前),`PUT /admin/channels {enabled, order}`(enabled 取当前 list 中 enabled 的 id,顺序按新 order)——与现有契约完全一致;
- **渲染排序**:`loginChannels`/`apikeyChannels` 计算属性改为按 `list` 中的顺序输出(list 本身就是 GET /admin/channels 的返回序,后端已按 saved order 排),即拖拽保存后重新 loadList 自然生效;本地先乐观重排再发 PUT;
- **其它模块下拉**:后端 GET /admin/channels 的 `channels`/`known` 已按 order 返回,前端不做额外排序即可遵守;验证 keys.js/usage.js/models.js 的下拉数据源直接用该顺序(它们已如此,不动)。

### 1.3 Sortable 注册
- `import Sortable from '../vendor/Sortable.min.js'`(检查该文件是否为 ESM;若是 UMD 挂 window,则直接用 `window.Sortable`——实现时确认,以能跑通为准);
- 两个 tbody 各建一个 Sortable 实例(group 不同),`onEnd` 里从 evt.from/to 读取新 DOM 顺序映射回 id 数组 → 乐观更新 list 顺序 → PUT。

### 1.4 杂项
- 行内按钮布局:`≡ 详情 | 开关`,均 `@click.stop`;
- README 双语若有"详情卡/列表"描述,核对措辞;无则跳过;
- 术语延续「登录型/密钥型」。

## 2. 校验与验收

- 校验三件套:node --check channels.js;check_roots.py 全页面 OK;pytest tests/test_custom_channels.py tests/test_custom_channels_gmi.py tests/test_custom_channels_bailian.py tests/test_web_assets.py tests/test_docs_encoding.py -q -p no:cacheprovider($env:PYTHONPATH="$PWD\src");
- 验收清单:
  - [ ] 详情 card 不再存在;两组每行有「详情」按钮,浮窗正确显示登录型/密钥型各自内容;
  - [ ] 密钥型详情浮窗内「编辑」能切到表单并保存生效;
  - [ ] 组内拖拽生效,刷新页面后顺序保持(后端持久化);
  - [ ] 密钥型组永远在登录型组之后;
  - [ ] keys/usage/models 页下拉顺序与通道管理页一致;
  - [ ] 行内「详情」与开关不触发行选中冲突;
  - [ ] 回归通过。

## 3. Out of Scope
- 后端 admin API 零改动;其余页面功能不动(仅确认下拉顺序);
- 不新增依赖;保持压缩单行风格。
