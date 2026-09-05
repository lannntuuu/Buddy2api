# 用量统计:回退新增列 + 命中率口径改为 命中/输入 Spec

- 分支:`feature/bailian-provider`(延续)
- 状态:待实现
- 用户需求(28 号 spec 的部分回退):
  1. **去掉刚加的两列**:「非缓存输入」「缓存创建 Token」从表格、顶部 metric 区、`noncached()` 函数全部移除;
  2. **缓存命中率公式改为 命中 token ÷ 输入 token**——即保持现有后端公式不变(cache_read ÷ prompt_tokens,本来就是"命中÷输入"),**改动落在文案与标识上**:RATIO_TIP 回到简明口径「缓存命中 Token ÷ 输入 Token」;命中率列**继续是计算列**(◆ 徽标保留);平均耗时 ◆ 保留;CACHE_INC_TIP(命中已含在输入中)保留在缓存命中列;
  3. 派给同一 subagent(ea30335d)。

## 1. 精确回退清单(channels 无关,仅 usage.js + app.css)

### 1.1 usage.js
- 表格列序回退为:`请求数 | 输入 Token | 缓存命中 Token ◆(口径 title) | 缓存命中率 ◆ | 输出 Token | 总 Token | Credit | 平均耗时 ◆`(8 数据列 + 首列,与 26 号交付一致);
  - 注意:**缓存命中 Token 列保留**(26 号加的,用户没说去掉),其 CACHE_INC_TIP 保留;但该列加 ◆?——不加,它是元数据直存,维持 26 号形态(仅 title 无徽标);
- 移除:`noncached()` 函数、`NONCACHED_TIP`/`CACHE_CREATE_TIP` 常量(含 return 导出)、三级行中的两个新列单元格、metric 区「非缓存输入」卡(网格恢复 `dash-grid thirds` 3 卡);
- `RATIO_TIP` 文案改为:`缓存命中 Token ÷ 输入 Token(prompt_tokens) — 命中部分已含在输入中;输入为 0 时不计算`;
- 表头徽标保留:缓存命中率 ◆(RATIO_TIP)、平均耗时 ◆(AVG_TIP);**缓存命中 Token 列不挂徽标**(元数据),仅保留其 th 上的 CACHE_INC_TIP。

### 1.2 app.css
- `.calc-mark` 保留(命中率/平均耗时仍在用);无其它改动。

### 1.3 tests
- 28 号若给 test_custom_channels.py 加过 noncached 相关前端断言——检查:28 号只改了 usage.js,测试无涉及,预计无测试改动;若 grep 到引用需同步清理。

## 2. 校验与验收
- 三件套:node --check usage.js;check_roots2 全页面 OK;pytest tests/test_web_assets.py tests/test_docs_encoding.py tests/test_custom_channels.py tests/test_custom_channels_gmi.py tests/test_custom_channels_bailian.py -q -p no:cacheprovider($env:PYTHONPATH="$PWD\src");
- 验收:
  - [ ] 表格回到 26 号列序(无非缓存输入/缓存创建列);metric 区回 3 卡;
  - [ ] RATIO_TIP 新文案生效(命中÷输入);命中率列 ◆ 保留;
  - [ ] 缓存命中 Token 列仍在(带口径 title,无徽标);
  - [ ] 平均耗时 ◆ 保留;
  - [ ] check_roots2 全页面 OK(移除的常量不得残留在模板引用);
  - [ ] 回归通过。

## 3. Out of Scope
- 后端零改动;logs.js 不动;.calc-mark 保留。
