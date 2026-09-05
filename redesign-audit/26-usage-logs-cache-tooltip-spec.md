# 用量统计/请求日志:缓存 Token 列 + 计算列公式悬浮提示 Spec

- 分支:`feature/bailian-provider`(延续)
- 状态:待实现
- 用户需求:
  1. 用量统计列表(及顶部指标区已展示)中,**缓存命中的 prompt_token 显示**——经核实:聚合数据里 `prompt_tokens` 已含缓存命中部分(上游口径),需要在缓存命中 Token 列让这个关系可见:缓存命中 Token 列加悬浮提示"已包含在输入 Token 中",并把**命中率公式**做成表头悬浮;
  2. **计算列的表头悬浮显示公式**(usage + logs 两个页面统一处理):凡值是计算而来的列,表头 `title` 显示公式;公式太长时用"简短式 + 完整式"常见处理——`title` 放一行简明公式(如 `命中 / 输入`),更长的语义说明放同一 title 内以 ` — ` 分隔(单行 title,不弹自定义气泡组件);
  3. subagent 用 hy3,新开一个(不是 fe6a5c91)。

## 1. 已核实的事实(实现依据)

- **usage.js**(`/admin/provider-model-usage` → stats.get_provider_model_usage):
  - 行结构:`summary`(平台/模型/日/总)与 `detail`(按日);`prompt_tokens`、`cache_read_tokens`、`cache_hit_ratio`(= cache_read_tokens / prompt_tokens,prompt>0 时,stats.py `_finalize`)、`avg_duration_ms`(= duration_ms/requests)均为**后端算好**或 SQL SUM,**前端只是展示**;
  - 列:请求数 / 输入 Token / 缓存命中 Token / 缓存命中率 / 输出 Token / 总 Token / Credit / 平均耗时;
  - Credit 行是 SUM(元数据);平均耗时是计算列;命中率是计算列;总 Token = SUM(total_tokens)(上游报的总和,视为元数据);
  - 页面现有 `title` 用量:1 处。已有 `pct()/tok()/money()/ms()/n()` helpers。
- **logs.js**(SELECT * 返回全行字段,`cache_read_tokens` 已在行数据里但前端未展示):
  - 列:时间/Key/账号/模型/思考/Client/流/Prompt/Completion/Token/Credit/耗时/状态;
  - 行内 Prompt/Completion/Token/Credit/耗时全部是**元数据**(logs 表直存);计算列不存在,但有现成 tooltip 先例:`reasoningTitle`、client 悬浮;
  - 页面无 `pct` helper。

## 2. 改动清单

### 2.1 usage.js——表头公式悬浮(核心)
统一做法:计算列的 `<th>` 加 `:title="..."`;非计算列不加。映射表(简式 — 语义):

| 列 | title 内容 |
|---|---|
| 缓存命中率 | `缓存命中 Token ÷ 输入 Token(prompt_tokens) — 上游 prompt 已含缓存命中部分;输入为 0 时不计算` |
| 平均耗时 | `总耗时 ÷ 请求数 — 全部请求的平均值,含失败` |
| 总 Token | `输入 + 输出 + 缓存创建等上游上报项的总和(元数据,不做前端计算)`——**若确认是元数据则不加 title**(以实际后端口径为准:total_tokens 是 logs 直存的 SUM,属元数据,**不加**) |
| Credit | 元数据(SUM(credit)),不加 |
| 请求数/输入/缓存命中/输出 | 元数据,不加 |

- 顶部 metric 区的"缓存命中率"卡片已有 `cache_read / prompt_tokens` 副标题,补齐与表头一致的完整 title;
- **缓存命中 Token 列**(表头)加 title:`已包含在输入 Token(prompt_tokens)中,非额外增量`——这是需求 1 的呈现方式(值本身已显示,悬浮说明口径)。

### 2.2 usage.js——明细行
- 平台汇总/模型小计/按日明细三行的单元格渲染不变(值已有);无行级 title 需求(表头统一说明)。

### 2.3 logs.js——无计算列,但补一处口径可见性
- Prompt 列表头加 title:`上游上报的 prompt_tokens;缓存命中部分已含其中(若上游回报)`(logs 行有 `cache_read_tokens` 字段但列表未展示——**按需求 1 精神补一列「缓存命中」**:`{{tok(x.cache_read_tokens)}}`,放在 Prompt 与 Completion 之间;该列属元数据,不加公式 title);
- Credit 列表头加 title:`上游上报的 credit;网关侧估算值(若启用 credit_rate)非真实扣费`;
- 无其它计算列。

### 2.4 通用
- title 全部用原生 `title` 属性(与 reasoningTitle 先例一致),不引入自定义气泡;
- 公式文案常量:usage.js 顶部 `const RATIO_TIP='...'`、`AVG_TIP='...'`、`CACHE_INC_TIP='...'`;logs.js 类似,避免模板内长字符串;
- 保持压缩单行风格;不新增依赖。

## 3. 校验与验收

- node --check usage.js logs.js;.tmp/check_roots2.py 全页面 OK(新常量须进 return);pytest tests/test_web_assets.py tests/test_docs_encoding.py tests/test_custom_channels.py tests/test_custom_channels_gmi.py tests/test_custom_channels_bailian.py -q -p no:cacheprovider($env:PYTHONPATH="$PWD\src");
- 验收:
  - [ ] usage 表头「缓存命中率」「平均耗时」悬浮显示公式;「缓存命中 Token」悬浮说明口径;元数据列无 title;
  - [ ] usage 顶部命中率卡片 title 与表头一致;
  - [ ] logs 表新增「缓存命中」列(值来自行数据),Prompt/Credit 表头有口径说明;
  - [ ] 无自定义气泡组件,原生 title;
  - [ ] 回归通过。

## 4. Out of Scope
- 后端端点/SQL 零改动(logs 行数据已含 cache_read_tokens);
- dashboard/quota 页不动;不新增统计指标。
