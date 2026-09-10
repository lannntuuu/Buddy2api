# 38 号 spec：SPA 运行时 ReferenceError 防线（API Keys 页二次修复）

## 0. 背景与事故链

### 0.1 用户报告（未解决）
```
API Keys 打开之后通道的数据没显示，下拉框也没内容，右上方还有显示「加载失败」
```

### 0.2 两处独立缺陷叠在一起

**缺陷 A（已在 3e543b4 修复）：父级注入函数漏传 prop。**
`73d334c` 把 `ensureChannels` 挂在根 setup 的 `return` 里，但模板没绑定、子页
`props` 也没声明。Vue 不会把父 setup 的局部变量透给子组件，于是
`p.ensureChannels(...)` 抛 `is not a function`；异常发生在 `l.value` 赋值之后，
列表被 catch 吞成空 + toast「加载失败」+ 通道下拉空。

**缺陷 B（本次修复，也是"修完还坏"的真凶）：`app.js` 里 `api` 是未声明的自由标识符。**

```js
// src/web/js/app.js
import {toastActionFor} from './api.js';   // L2 —— 只导入了 toastActionFor，没有 api
...
try{const ch=await api.get('/admin/channels',token); ...}   // L56 —— api 未定义
```

实测（`node` 解析 import 绑定）：
```
app.js bound imports: I, chns, dash, keys, lgs, mdls, quota, setup, stgs, toastActionFor, usg
does app.js import `api`? -> false
does app.js use `api.`? -> true
local `const api`/`let api`/`function api`? -> false
```

`api` 既不是 import、不是局部声明、也不是全局（`index.html` 无 `window.api`，
全仓 grep 无 `window.api` / `globalThis.api`）。ESM 是严格模式，赋值给未声明
变量抛 `ReferenceError`；此处是**读取**未声明标识符，同样抛 `ReferenceError`。

缺陷 A 修完后 `ensureChannels` 能被调用了，但它**第一行就炸在 `api.get` 上**，
catch 吞掉 → `sharedChannels=[]` → 下拉仍空、症状一字不差。这就是"还是没处理好"。

### 0.3 为什么既有测试全绿放行

`tests/test_web_assets.py::test_js_module_parses` 用 `vm.SourceTextModule` 只做
**语法解析**，不执行、不做作用域/绑定检查。ESM 里引用一个未声明的全局标识符
在**解析期完全合法**（运行时才可能是 `ReferenceError` 或恰好命中某个全局）。
因此这一类"能解析但跑不起来"的缺陷是该测试的**结构性盲区**。

## 1. 目标

1. **P0**：修掉 `app.js` 的 `api` 未导入，让 API Keys / 用量统计页真正可用。
2. **P0**：补一道**能抓住这一类缺陷**的自动化防线（解析期 + 静态绑定检查），
   使"引用了未 import 的模块符号"在 CI 里失败，而不是等用户在浏览器里发现。
3. **P1**：不引入构建步骤、不引入新依赖（仓库是零构建 vendored Vue SPA）。

## 2. 非目标

- 不引入打包器 / TypeScript / ESLint（违反零构建约束）。
- 不做完整的数据流/类型分析；只做**模块符号绑定**层面的检查。
- 不改动既有页面视觉与交互。

## 3. 修复方案

### 3.1 代码修复（P0）

`src/web/js/app.js` L2 补齐 `api`：

```js
import {api,toastActionFor} from './api.js';
```

这是唯一必需的运行时改动。

### 3.2 防线（P0）：两道检查

#### 防线 1 —— ESM 链接期检查（能抓"引用了不存在的导出名"）

把既有 parse driver 从 `new SourceTextModule(...)` 提升到 **`module.link()`**。
ESM 的 link 阶段会解析 import/export 绑定：若 import 的符号在目标模块里不存在
（例如 `import {api} from './api.js'` 而 api.js 没导出 api），link 会抛
`SyntaxError: The requested module does not provide an export named 'api'`。

注意：link **不能**抓"忘了 import 直接用自由标识符"（缺陷 B 属于此类）——
那是运行期 `ReferenceError`。所以必须叠加防线 2。

#### 防线 2 —— 静态绑定检查（抓"用了但没 import"）

在 `tests/test_web_assets.py` 加一条断言，对 `src/web/js/**/*.js`：

1. 收集每个文件的**已绑定 import 名**（含 `as` 别名取别名、default import）。
2. 收集该文件里**引用到的跨模块共享符号**（`api.js` / `format.js` / `icons.js`
   的导出名清单，可由正则从这三个文件的 `export` 声明中提取，避免硬编码漂移）。
3. 若某符号在本文件被**当作值引用**（前面不是 `.`，排除 `x.api` / `obj.api`
   这类属性访问）且未被 import、也不是本文件自己的导出、也没有局部声明
   （`const/let/var/function/class` 或解构）→ 失败。

**误报控制（必须逐条验证，见 §5）**：
- 排除本文件自身导出的符号（如 `api.js` 里用 `apiErr` 是它自己导出的）。
- 排除 `obj.api` / `props.api` 形式：引用前面紧跟 `.` 的不算。
- 排除模板字符串/Vue 模板里的文本（模板是字符串，不是 JS 作用域；
  但需注意 `${}` 插值内是真 JS —— 用"仅扫描非模板区域 + 插值单独扫"或
  直接要求：命中必须形如 `name(` 或 `name.` 或 `name,` 等**代码位置**，
  模板纯文本里几乎不会出现 `api.` 这种形态，实测可控）。
- 排除注释里的文字：先剥离 `//` 与 `/* */`。

### 3.3 可选防线 3（P2，本次不强制）

起一个真实服务 + `fetch` 各 `/static/js/*.js` 做冒烟；但 `ReferenceError`
只在**执行**时炸，静态 fetch 抓不到，需要 headless 浏览器。仓库无 playwright，
本次不做，记入遗留。

## 4. 验收门禁

1. `pytest tests/test_web_assets.py -q` 全绿。
2. **变异验证（必做）**：
   - 把 `app.js` 的 `import {api,toastActionFor}` 改回 `{toastActionFor}` →
     新增的绑定检查必须 **FAIL**。
   - 在 `app.js` 里加一行 `import {nonExistent} from './api.js'` →
     link 检查必须 **FAIL**。
   - 两处变异各自 revert 后恢复全绿。
3. 浏览器实测：API Keys 页列表有数据、通道下拉有内容、无「加载失败」toast；
   用量统计页平台下拉有内容。（`/static` 有 1h 缓存，需硬刷）

## 5. 风险与验证要求

- 防线 2 是**正则启发式**，最大风险是误报。实现者必须先跑一次全量扫描，
  确认在**修复后的代码库上零误报**，再落地断言；若个别符号确有合理误报，
  加**具名白名单**并注明原因，不允许整体放宽。
- 注意 `withBusy as withBusyR`：绑定名是别名 `withBusyR`，检查"用了 `withBusy`"
  时必须以别名为准（keys/channels/quota 三页都这样用），否则误报。
- `logs.js` 用 `fmtSec as fmt`：同理。
- ESM link 需要 node 的 `--experimental-vm-modules`（既有 driver 已带）。

## 6. 交付物

- `src/web/js/app.js`：补齐 `api` 导入。
- `tests/test_web_assets.py`：新增 ESM link 检查 + 绑定检查两条断言。
- 本报告顶部的变异验证记录。
- 提交信息说明根因（缺陷 B）与为何既有测试漏过。
