# 用量统计:非缓存输入列 + 命中率口径修正 + 计算列标识 Spec

- 分支:`feature/bailian-provider`(延续)
- 状态:待实现
- 用户需求:
  1. 用量统计增加 **prompt_token 列**——经核实,语义上这是"**非缓存输入 Token**"(= prompt_tokens − cache_read_tokens),它才是真正按原价计费的部分;同时修正命中率口径疑虑;
  2. **缓存命中率计算有误**——核实结论:公式 `cache_read ÷ prompt_tokens` 本身正确(cache_read 是 prompt 的子集,clamp 已保证,见 store_common.extract_cache_tokens 文档),**误的观感来自分母口径**:用户直觉的"命中率"是命中占**真实输入**的比例,且"输入 Token"列显示的是含缓存的总 prompt,两者并排就变成了 `命中率 ≠ 命中/输入` 的错觉。修正 = 拆列展示,让公式自解释;
  3. **非元数据表头加特殊标识**(◆ 计算列徽标),与悬浮 title 双通道;
  4. 派给 subagent(hy3,新开)。

## 1. 已核实事实

- stats.py `_finalize`:`cache_hit_ratio = cache_read_tokens / prompt_tokens`(prompt>0;cache_read 在入库时已 clamp 到 [0, prompt_tokens],是 prompt 的子集——公式数学上正确);
- usage.js 表列:请求数 / 输入 Token(prompt_tokens) / 缓存命中 Token / 缓存命中率 / 输出 Token / 总 Token / Credit / 平均耗时;数据后端算好;
- 无 `cache_creation_tokens` 列展示(数据有,WorkBuddy/Anthropic 风格才有值);
- `pct()` 来自 api.js:`(v).toFixed(整数?0:1)+'%'`;
- 27 号 spec 已给计算列加 title(RATIO_TIP/AVG_TIP/CACHE_INC_TIP);元数据列无 title;**尚无视觉徽标**;
- docs/design/cache-stats-and-reasoning-display-spec.md 明确口径:prompt_tokens 含缓存命中部分(OpenAI/DeepSeek/Anthropic 通用语义)。

## 2. 改动清单

### 2.1 列重组(usage.js 表格 + 顶部 metric)

新列序:`请求数 | 输入 Token(总) | 非缓存输入 | 缓存命中 | 缓存创建 | 缓存命中率 | 输出 Token | 总 Token | Credit | 平均耗时`

- **「非缓存输入 Token」(新列)**:`prompt_tokens − cache_read_tokens`,三级行(平台汇总/模型小计/按日)均显示;**前端计算**(row 数据已有两个字段,减法在渲染层做,或后端加字段——取前端减法,零后端改动;负值 clamp 0 防御);
- **「缓存命中率」口径修正**:分母改为**非缓存输入 + 缓存命中**的比值?**不**——保持后端公式(命中/总 prompt)不动,但表头 title 改写为两级:`命中占输入总量的比例 = 缓存命中 ÷ (非缓存输入 + 缓存命中);总输入 prompt_tokens 已含命中部分`。观感修正来自"非缓存输入"列把分子分母的关系摆清楚;
- **「缓存创建 Token」(新列)**:数据已有(cache_creation_tokens),Anthropic 风格上游才有值,其它平台显示 0;放缓存命中列旁;
- 顶部 metric 卡片:增加「非缓存输入」数值;命中率卡片 subtitle 同步新文案。

### 2.2 计算列徽标(核心)
- **后端算好但非直存的列**与**前端减法列**都算"非元数据":缓存命中率、平均耗时、**非缓存输入**(新);
- 表头加 `<span class="calc-mark" title="该列为计算值:...">◆</span>`(◆ U+25C6,现有字体栈可显示;title 复用 RATIO_TIP/AVG_TIP 与新 tip 常量)——**徽标 + 原有悬浮 title 并存**(27 号已建 title,本次补视觉标识,且把 title 移到徽标上,th 本身不再挂 title,避免双 tooltip);
- 元数据列不加任何标识;
- app.css 新增 `.calc-mark{color:var(--warn);cursor:help;font-size:10px;margin-left:4px;vertical-align:middle}`(黄系=注意这是推导值;cursor:help 语义化)。

### 2.3 常量更新(usage.js)
- `RATIO_TIP` 文案更新(见 2.1);
- 新 `NONCACHED_TIP`:`输入 Token(prompt_tokens) − 缓存命中 — 未命中缓存、按原价计费的部分`;
- 新 `CACHE_CREATE_TIP`:`上游上报的缓存写入 token(仅 Anthropic 风格上游提供;其它平台为 0)`;
- AVG_TIP 不变;全部进 setup return(check_roots2 会校验)。

### 2.4 logs.js
- 不加列(logs 行是单请求元数据,无聚合列;prompt/cache_read 字段已在 27 号加了说明 title)——本 spec 仅 usage 表格。

## 3. 校验与验收

- node --check usage.js;check_roots2 全页面 OK;pytest tests/test_web_assets.py tests/test_docs_encoding.py tests/test_custom_channels.py tests/test_custom_channels_gmi.py tests/test_custom_channels_bailian.py -q -p no:cacheprovider($env:PYTHONPATH="$PWD\src");
- 验收:
  - [ ] 新列序:请求数/输入 Token/非缓存输入/缓存命中/缓存创建/缓存命中率/输出/总 Token/Credit/平均耗时;
  - [ ] 非缓存输入 = 输入 − 缓存命中(抽查一行手算一致,负值显示 0);
  - [ ] 缓存创建列显示(多数平台 0);
  - [ ] ◆ 徽标出现在:非缓存输入、缓存命中率、平均耗时三列表头,悬浮显示对应公式;元数据列无徽标;
  - [ ] 顶部 metric 区新增「非缓存输入」,命中率卡片文案更新;
  - [ ] 回归通过。

## 4. Out of Scope
- 后端 SQL/端点零改动(减法在渲染层);
- logs.js 不动;
- pct() helper 不改(命中率仍由后端算好传入);
- dashboard 不动。
