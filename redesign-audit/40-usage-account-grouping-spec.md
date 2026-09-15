# 40 号 spec：用量统计「按账号分组」维度（可勾选开关）

- 分支：`main`
- 状态：待实现（契约已冻结，实现者不得自行改口径）
- 上游需求（用户原话）：「不同通道如果有多个账号，我想在用量统计中看到针对账号的分组，而且这个分组最好是可以勾选和取消勾选的。」
- 用户已拍板的两个细节：
  1. 勾选框的语义 = **控制「账号」这一维度是否展开**（不是筛选、不是排除某账号）；
  2. **仅多账号通道**显示账号层，单账号通道保持现有三段式；
  3. **默认勾选**，并用 `localStorage` 记忆上次选择；
  4. 展开时**两级小计都显示**（平台级模型小计 + 账号内模型小计）。

---

## 0. 为什么这么设计（约束与不可违反的边界）

- `logs` 表**已经有** `account_id` / `account_name`（`src/storage/repos/logs.py` 写入，
  `account_name` 是请求时的值拷贝，账号被删后历史归属仍在）。**零 schema 变更。**
- **零新增路由**：不改 `/admin/provider-model-usage`（`tests/test_route_golden.py` 冻结路由集，
  新增路由会红灯）。
- **零新增查询参数**：勾选切换是纯前端行为，不发请求 —— 因此不消耗
  `_check_usage_rate_limit`（默认 30 次/分钟）的额度，切换零延迟。
- 顶部三张卡片、`totals`、各通道 `summary` 的**数字与口径任何情况下都不变**。
  账号层是同一批行的另一维展开，**不是筛选**。
- 现有返回结构（`providers[p].models[m].daily` / `.summary`）**只增不改**，
  `tests/test_provider_model_usage.py` 已有断言必须原样通过。

---

## 1. 后端契约（`src/storage/repos/stats.py`）

### 1.1 返回结构新增字段

```jsonc
"providers": {
  "workbuddy": {
    "summary": { /* 不变：该通道全账号合计 */ },
    "models":  { "glm-5.3": { "daily": [ /* 不变 */ ], "summary": { /* 不变 */ } } },
    "account_count": 2,          // 新增：时间窗内该通道 distinct 账号数
    "accounts": [                // 新增：仅 account_count >= 2 时出现该键
      {
        "id": 1,
        "name": "图图",
        "summary": { /* 与平台层同口径，同一 _finalize */ },
        "models": { "glm-5.3": { "daily": [ /* 与平台层同形状 */ ], "summary": {} } }
      },
      { "id": 11, "name": "18127098842", "summary": {}, "models": {} }
    ]
  },
  "traesolo": { "summary": {}, "models": {}, "account_count": 1 }   // 无 accounts 键
}
```

### 1.2 四条硬性规则

1. **`accounts` 必须是数组**。若用 `account_id` 当 dict key，JS `Object.keys` 会把纯数字键
   重排到最前，展示顺序会乱。数组天然保序。
   排序：`requests` 降序，再 `id` 升序（`id` 为 `None` 时按 0 参与比较）。
2. **`account_count` 与 `model` 筛选解耦**。它由**独立的第二条廉价查询**得出，条件只含
   时间窗（+ 可选 `provider`），**不含 `model`**。否则用户筛到某个模型、恰好只有一个账号
   跑过它时，账号层会莫名消失。
3. **`account_id IS NULL` 归一为「未指定账号」一行**（真实库 `test` 通道已有此类行）。
   归一键用字符串 `"none"`；显示名 `未指定账号`。`account_id` 非空但 `account_name`
   为空的，显示名回落为 `账号 #{id}`。
   账号名取值：`rows` 按日期降序，**取首个非空 `account_name`**（即最近的账号名）。
4. **可加和**：`Σ 各账号 summary == 平台 summary`（requests / tokens / credit 三个字段
   至少）。账号层复用同一个 `_finalize`，因此 `tps` / `cache_hit_ratio` /
   `avg_duration_ms` / `credit` 与平台层**完全同口径**。

### 1.3 实现要点（照此写，避免踩坑）

- 主查询的 `SELECT` 与 `GROUP BY` 各追加 `account_id, account_name`。
  现有 `ORDER BY date DESC, provider ASC, model ASC` **保持不变**（1.2 规则 3 依赖它）。
- **`conn.close()` 必须移到第二条查询之后**（现在紧跟在主查询 `fetchall()` 后面，
  直接加第二条查询会拿到已关闭的连接）。主查询与账号数查询共用同一个 `conn`。
- 账号数查询：

  ```sql
  SELECT provider, COUNT(DISTINCT COALESCE(account_id, -1)) AS n
  FROM logs{count_sql_where} GROUP BY provider
  ```

- 把「date 过滤是否生效」抽成 `has_start` / `has_end` 两个布尔量，供两条查询复用
  （原实现是内联的 `start not in (None, "", "all")`，语义必须保持等价）。
- 通道桶里先用一个临时键 `_accounts`（dict，键 `str(account_id)` 或 `"none"`）挂账号桶，
  出参前 **`pop("_accounts")` 掉**，绝不能泄漏到 JSON 里。账号桶内部的临时键 `_named`
  同理 —— 出参时按下文的显式 dict 构造，`_named` 自然不会带出去。
- 账号桶的 `daily` 列表复用刚 append 进模型桶的那个 dict 的**浅拷贝**
  （`dict(model_bucket["daily"][-1])`），不要重新计算一遍分母。

### 1.4 目标实现（可直接照抄这段逻辑）

主循环内、在 `_add(model_bucket["summary"], row)` 之前插入：

```python
        # 账号维度：同一批行多建一层桶；NULL 归一到「未指定账号」。
        aid = row["account_id"]
        akey = str(aid) if aid is not None else "none"
        accounts = prov_bucket["_accounts"]
        if akey not in accounts:
            accounts[akey] = {
                "id": aid,
                "name": f"账号 #{aid}" if aid is not None else "未指定账号",
                "_named": False,
                "models": {},
                "summary": _new_summary(),
            }
        acct = accounts[akey]
        raw_name = str(row["account_name"] or "").strip()
        if raw_name and not acct["_named"]:
            # rows 按日期降序：首个非空名字即最近的账号名
            acct["name"] = raw_name
            acct["_named"] = True
        if m not in acct["models"]:
            acct["models"][m] = {"daily": [], "summary": _new_summary()}
        acct_model = acct["models"][m]
        acct_model["daily"].append(dict(model_bucket["daily"][-1]))
        _add(acct_model["summary"], row)
        _add(acct["summary"], row)
```

出参汇总段（替换现有 `for prov_bucket in providers_out.values():` 那个循环）：

```python
    for p, prov_bucket in providers_out.items():
        for model_bucket in prov_bucket["models"].values():
            model_bucket["summary"] = _finalize(model_bucket["summary"])
        prov_bucket["summary"] = _finalize(prov_bucket["summary"])
        accounts = prov_bucket.pop("_accounts", {})
        for acct in accounts.values():
            for model_bucket in acct["models"].values():
                model_bucket["summary"] = _finalize(model_bucket["summary"])
            acct["summary"] = _finalize(acct["summary"])
        account_count = int(account_count_by_provider.get(p) or 0) or len(accounts)
        prov_bucket["account_count"] = account_count
        if account_count >= 2 and accounts:
            ordered = sorted(
                accounts.values(),
                key=lambda a: (
                    -a["summary"]["requests"],
                    a["id"] if a["id"] is not None else 0,
                ),
            )
            prov_bucket["accounts"] = [
                {
                    "id": a["id"],
                    "name": a["name"],
                    "summary": a["summary"],
                    "models": a["models"],
                }
                for a in ordered
            ]
```

通道桶初始化改为：

```python
            providers_out[p] = {"models": {}, "summary": _new_summary(), "_accounts": {}}
```

---

## 2. 前端契约（`src/web/js/pages/usage.js`）

### 2.1 勾选框

- 位置：`control-row` 的**最右侧**（查询按钮之后）。它是展示维度开关，不是筛选器，
  所以与左侧筛选簇保持一屏之隔。
- 结构：

  ```html
  <label class="acct-toggle" :title="hasMultiAcct?ACCT_TIP:'当前所选范围内没有多账号通道'">
    <input type="checkbox" v-model="showAccounts" :disabled="!hasMultiAcct"/>
    <span>按账号分组</span>
  </label>
  ```

- **默认勾选**；状态持久化到 `localStorage` 键 `cb_gw_usage_acct`
  （写 `'0'` / `'1'`，与既有 `cb_gw_theme` / `cb_gw_rail` / `cb_gw_page_v2` 同一模式，
  读失败一律按默认 `true`，全部包 `try/catch`）。
- 无任何多账号通道时**置灰**（`:disabled`）但**不隐藏**，避免布局跳动。

### 2.2 渲染树（勾选开启时）

以 workbuddy（2 个账号）为例，**缩进即层级**：

```
glm-5.3 · 小计            ← L1 平台级模型小计（padding-left:20px）
hy3 · 小计                ← L1 平台级模型小计
图图 · 账号小计            ← L1 账号小计（padding-left:20px）
  glm-5.3 · 小计           ← L2 账号内模型小计（padding-left:40px）
    09-10                  ← L3 日明细（padding-left:60px）
    09-09
18127098842 · 账号小计
  glm-5.3 · 小计
    09-08
workbuddy · 平台汇总       ← L0 该通道全账号合计
```

> **与需求确认时那张示意图的唯一差异（刻意为之，不得"修正"）**：
> 示意图把「平台汇总」画在最上方，但**现状代码就是把平台汇总 push 在每个通道块的末尾**
> （`usage.js` 现有 `flatRows` 在模型循环结束后才 push 平台汇总），且需求的硬约束是
> 「勾选关闭时与今天逐字节一致」。因此本 spec 规定：**沿用现有「每组小计落在组末尾」
> 的既有排版**，账号块同理（账号小计落在账号块末尾）。嵌套关系与示意图完全一致，
> 变的只是「小计行在组内的位置」，且与勾选关闭时的行为保持一致。

勾选**关闭**时：输出与今天**逐字节一致**（`平台汇总 → 模型小计 → 日期明细`），
行为完全回退。

**单账号通道**：无论勾选与否，都保持三段式，一行不加。

### 2.3 `flatRows` 目标实现

```js
const hasMultiAcct=computed(()=>{const provs=data.value?.providers||{};return Object.keys(provs).some(p=>(provs[p].account_count||0)>=2&&(provs[p].accounts||[]).length>0)});
const flatRows=computed(()=>{
  const out=[];const provs=data.value?.providers||{};const want=showAccounts.value&&hasMultiAcct.value;
  for(const prov of Object.keys(provs)){
    const bucket=provs[prov];
    // 每个通道独立判定：多账号通道展开账号层，单账号通道维持三段式（同表混排）
    const grouped=want&&(bucket.accounts||[]).length>0;
    for(const mdl of Object.keys(bucket.models||{})){
      const m=bucket.models[mdl];
      out.push({kind:'model',lvl:1,prov,mdl,summary:m.summary});
      if(!grouped)for(const d of (m.daily||[]))out.push({kind:'day',lvl:2,prov,mdl,detail:d});
    }
    if(grouped){
      for(const a of bucket.accounts){
        for(const mdl of Object.keys(a.models||{})){
          const m=a.models[mdl];
          out.push({kind:'amodel',lvl:2,prov,acct:a,mdl,summary:m.summary});
          for(const d of (m.daily||[]))out.push({kind:'day',lvl:3,prov,acct:a,mdl,detail:d});
        }
        out.push({kind:'acct',lvl:1,prov,acct:a,summary:a.summary});
      }
    }
    out.push({kind:'prov',lvl:0,prov,summary:bucket.summary});
  }
  return out;
});
```

**注意**：展开判定必须是**每通道**的（`grouped`），不能是一个全局布尔 —— 否则
`want` 为真但某通道没有 `accounts` 时，该通道的日明细会被整段吞掉。

### 2.4 模板改动

`<tbody>` 里的 `v-for` 由「靠 `row.mdl`/`row.detail` 区分」改为**靠 `row.kind` 区分**
（更直白，且新增两种行型）：

```html
<template v-for="(row,i) in flatRows" :key="i">
  <tr v-if="row.kind==='prov'" class="prov-row"><td style="font-weight:800">{{row.prov}} · 平台汇总</td>…（10 列，同现状）</tr>
  <tr v-else-if="row.kind==='acct'" class="acct-row"><td style="font-weight:600;padding-left:20px" :title="'账号 ID '+(row.acct.id??'-')">{{row.acct.name}} · 账号小计</td>…（10 列）</tr>
  <tr v-else-if="row.kind==='amodel'" class="model-row"><td class="mono" style="padding-left:40px">{{row.mdl}} · 小计</td>…（10 列）</tr>
  <tr v-else-if="row.kind==='model'" class="model-row"><td style="font-weight:600;padding-left:20px">{{row.mdl}} · 小计</td>…（10 列）</tr>
  <tr v-else-if="row.kind==='day'"><td class="mono" :style="'padding-left:'+(row.lvl>=3?60:32)+'px'">{{row.mdl}} · {{row.detail.date}}</td>…（10 列）</tr>
</template>
```

- 10 个数据列的单元格内容与现有代码**完全一致**，只是数据源从
  `row.summary` / `row.detail` 分出 `acct`/`amodel` 分支。**不要**顺手改列序、
  不要动 `RATIO_TIP` / `AVG_TIP` / `CACHE_INC_TIP` / `TPS_TIP`。
- 新增口径常量 `ACCT_TIP`（见 2.5），**必须进 `setup` 的 `return`**，
  否则 `.tmp/check_roots2.py` 会报 MISSING ROOTS。

### 2.5 文案

```js
const ACCT_TIP='按请求日志里的账号(request logs.account_name)归集;仅多账号通道展开,单账号通道保持平台→模型→日期三段式;只改变展示维度,平台小计与顶部合计口径不变';
```

### 2.6 `setup` 返回值新增

`showAccounts`、`hasMultiAcct`、`ACCT_TIP`。
（`flatRows` 已在返回值里，只是实现换了。）

---

## 3. CSS（`src/web/css/app.css`）

紧邻现有 `.prov-row` / `.model-row` 规则追加（`tests/test_web_assets.py::
test_css_covers_all_used_classes` 会强制「模板里用到的类名必须在 app.css 有选择器」）：

```css
.acct-row td{background:var(--bg-sunken);border-top:1px solid var(--border)}
.acct-toggle{display:inline-flex;align-items:center;gap:6px;font-size:var(--fs-b);
  color:var(--fg-2);cursor:pointer;user-select:none;white-space:nowrap}
.acct-toggle input{accent-color:var(--accent);cursor:pointer;margin:0}
.acct-toggle input:disabled{cursor:not-allowed}
.acct-toggle input:disabled+span{opacity:.5}
```

**不要**新增其它类名，**不要**改既有规则。仅用上表已有的 CSS 变量
（`--bg-sunken` / `--border` / `--fs-b` / `--fg-2` / `--accent` 均已存在）。

---

## 4. 测试

### 4.1 后端（`tests/test_provider_model_usage.py`）

先把现有 `_add_log` helper 扩展为可传账号，**默认值保持 `None`**，使既有用例零改动：

```python
def _add_log(provider: str, model: str, created_at: int, *, prompt: int = 100,
             completion: int = 50, credit: float = 0.1, duration_ms: int = 1000,
             first_token_ms: int | None = None,
             account_id: int | None = None, account_name: str | None = None):
```

新增 5 条（全部走 `db.get_provider_model_usage(...)` 直调，沿用 `isolated_db` fixture）：

1. `test_usage_accounts_only_for_multi_account_channels`
   —— qclaw 上 2 个账号、qwenwork 上 1 个账号：
   - `qclaw` 有 `accounts` 键且 `len == 2`，`account_count == 2`；
   - `qwenwork` **没有** `accounts` 键，`account_count == 1`；
   - `accounts[0]` 是 requests 更多的账号（降序断言）。
2. `test_usage_account_summaries_sum_to_provider`
   —— `Σ accounts[i].summary[field]` == `providers[p].summary[field]`，
   field ∈ {requests, prompt_tokens, completion_tokens, total_tokens, credit}；
   且每个账号内 `Σ models[*].summary.requests == accounts[i].summary.requests`。
3. `test_usage_accounts_survive_model_filter`
   —— 同通道 2 个账号，只有账号 A 跑过 `m1`、只有账号 B 跑过 `m2`；
   查 `{"provider": ..., "model": "m1"}` 时 `accounts` **仍然存在**且 `len == 1`，
   而 `account_count == 2`（这条钉死 1.2 规则 2）。
4. `test_usage_null_account_grouped_as_unspecified`
   —— 两条 `account_id=None` 的日志：`accounts` 出现，其 `id is None`、
   `name == "未指定账号"`。
5. `test_usage_account_level_tps_and_ratio_same_scope`
   —— 两个账号各写一条带 `first_token_ms` 的流式日志：
   账号层 `tps` 与平台层 `tps` 同口径（池化 `Σ输出/Σ解码`），
   `cache_hit_ratio` 与平台层一致。

### 4.2 前端（`tests/test_web_assets.py`）

新增一条静态断言测试（沿用该文件既有 `_read` / `WEB_JS` 风格），例如
`test_usage_account_grouping_ui`：

- `pages/usage.js`：含 `acct-toggle`、`ACCT_TIP`、`showAccounts`、`hasMultiAcct`、
  `account_count`、`accounts`、`cb_gw_usage_acct`；
- `css/app.css`：含 `.acct-toggle` 与 `.acct-row`。

---

## 5. 文档

- `docs/provider-model-usage.md`：
  - 「返回结构」JSON 里补 `account_count` / `accounts` 两个字段（含结构与条件说明）；
  - 「前端」小节补：明细表支持**按账号分组**（默认勾选、可取消、状态记忆），
    仅多账号通道展开 `平台汇总/账号小计/账号内模型小计/日明细`，单账号通道不变；
  - 「数据说明」补一句：账号归属取自日志行的 `account_name` 值拷贝，
    账号被删除后历史归属仍保留。
- `README.md`：在 t/s 那段（约 385–389 行）附近补一条**简短**中文说明。
- `README_EN.md`：对应补一句英文说明（保持两本 README 同步）。

**文档硬约束**（`tests/test_docs_encoding.py` 会验）：
两本 README 必须是严格 UTF-8、无 BOM、**LF 行尾（禁止 CR）**、
无 PUA 字符、无 U+FFFD；`README.md` 中文字符数 **≥ 4000**、行数 **≥ 400**。
**禁止**整文件重写导致行尾变成 CRLF。

---

## 6. 验收门禁

在仓库根目录执行（Windows / pwsh）：

```powershell
$env:PYTHONPATH="$PWD\src"
.venv\Scripts\python.exe -m pytest tests/test_provider_model_usage.py tests/test_cache_reasoning_stats.py tests/test_web_assets.py tests/test_route_golden.py tests/test_docs_encoding.py -q -p no:cacheprovider
node --check src\web\js\pages\usage.js
.venv\Scripts\python.exe .tmp\check_roots2.py
node --check src\web\js\pages\logs.js   # 确认未被误改
```

全部必须通过：
- pytest 全绿（**基线：这 5 个文件此前 74 passed**，新增用例后应为 74 + 新增数）；
- `check_roots2.py` 输出**每个页面 OK**、退出码 0；
- `usage.js` 语法检查通过。

### 6.1 额外自查（变异验证）

- 把 `accounts` 的排序改成升序 → 用例 1 的降序断言必须 FAIL。
- 把 `account_count` 查询加上 `model` 条件 → 用例 3 必须 FAIL。

---

## 7. 明确不做（Out of Scope）

- 不改 `dashboard.js` / `logs.js` / `quota.js` / 其它任何页面。
- 不做「按 API Key 分组」。
- 不新增任何后端筛选参数（`account_id` 不进 querystring）。
- 不改 `totals`、不改平台 `summary`、不改数据库结构、不改
  `/admin/provider-model-usage` 的路由签名。
- 不改 `RATIO_TIP` / `AVG_TIP` / `CACHE_INC_TIP` / `TPS_TIP` 既有文案。
- 不改列序、不改 10 个数据列的单元格内容与格式函数
  （`n` / `tok` / `pct` / `money` / `ms` / `fmtTps`）。
