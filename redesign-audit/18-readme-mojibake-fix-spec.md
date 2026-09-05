# README 乱码修复 Spec

- 分支:`feature/bailian-provider`(在当前工作区继续,勿切分支)
- 状态:待实现
- 目标文件:`README.md`、`README_EN.md`(仓库根目录)

## 1. 故障定性(已完成的取证结论)

两个 README 处于 **GBK 双重编码 + 部分字节永久丢失** 的乱码状态:

1. 历史上某次写入把 UTF-8 文本按 GBK 误解码后再以 UTF-8 落盘,产生 `涓按湰鏈...` 形态的 mojibake;
2. 后续又有过一次**有损**转码:约 420 个字符变成了 PUA 私用区字符(`U+E000–U+F8FF`,如 `\ue224`),原始字节已丢失;
3. 实测:96.7% 字符可按 `字符 → encode('gbk') → decode('utf-8')` 机械反转还原;**730 个字符(3.3%)不可逆**,只能按上下文重建;
4. `docs/` 目录下全部文档编码完好,可作术语参照;远端 GitHub(origin/main、prod、raw.githubusercontent.com)与本地 blob 字节级一致,**不存在干净来源可拉取**;
5. 好消息:Markdown 结构(标题层级、表格线、代码块、链接、英文标识符)100% 完好,损坏仅限中文字符本身。

## 2. 修复策略:机械反转 + 上下文重建

### 2.1 第一步:生成反转草稿

写一次性脚本 `ops/scripts/oneoff/revert_readme_mojibake.py`(归档到 oneoff,不许被业务 import):

- 逐字符尝试 `ch.encode('gbk')`;成功的字符进字节流,失败的(PUA/不可编码)以占位符 `「?」` 进字节流边界断开处;
- 按"可编码连续段"分段:`bytes_segment.decode('utf-8', errors='replace')`,段间用原字符拼接;
- 输出 `README.reverted.md` / `README_EN.reverted.md` 草稿到 `.tmp/`(不进 git)。

### 2.2 第二步:人工级重建(核心工作量)

以草稿为底稿,**逐节**重写两个 README 的最终版,要求:

1. **不可逆占位处按上下文重建**:730 个丢失字符大多是常用词("本机已登录的消费级 AI 客户端"之类),结合句意、表格对应关系、`docs/` 下完好文档(如 `docs/design/multi-channel-v2.md`、`docs/credit-and-token-tracking.md`)的术语用法重建;吃不准的措辞宁可用保守表达,不得编造事实;
2. **结构保真**:保留全部标题、表格(行数列数不变)、代码块、链接、锚点;表格里的英文标识符(`CB_*`、路径、URL)一字不动;
3. **内容基准**:以当前工作区 README 为唯一内容基准——它包含最新事实(v2.2、Bailian 渠道行、`CB_BAILIAN_API_KEY` 行、src/ 目录结构);反转草稿只用来恢复"怎么写",不引入旧版本内容;
4. **README_EN.md**:它是英文文档,仅少量中文字符(426 个 zh、1 个 PUA)混在英文里;同样反转+重建,语言保持英文;
5. **事实校验**:README 里描述的命令、路径、环境变量名,与仓库实际文件(如 `src/gateway/server.py` 的参数、`ops/` 脚本)抽查核对;发现文档与代码不符时以代码为准修正,并在最终报告里列出修正点。

### 2.3 编码红线(硬性要求)

- 最终文件必须是**无 BOM 的 UTF-8**:写完后用 Python 验证前 3 字节 ≠ `EF BB BF`,全文 `decode('utf-8', errors='strict')` 通过;
- 全文不得含 PUA(`U+E000–U+F8FF`)、不得含 `U+FFFD`;
- 行尾保持 LF(git 会按 core.autocrlf 处理,无需刻意转 CRLF)。

## 3. 防回归测试:`tests/test_docs_encoding.py`

新建测试,断言:

1. `README.md`、`README_EN.md` 可 strict UTF-8 解码;
2. 无 BOM(读原始字节判断);
3. 无 PUA 与 `U+FFFD`;
4. `README.md` 含 `# Buddy2api`、`Bailian`、`CB_BAILIAN_API_KEY`、`CB_GATEWAY_PROVIDERS`;中文字符数 ≥ 4000(防内容被清空式"修复");
5. `README_EN.md` 含 `# Buddy2api`、`Bailian`;
6. markdown 表格行数与修复前一致可放宽,但 `README.md` 行数 ≥ 400(防大段删除)。

运行:`python -m pytest tests/test_docs_encoding.py -q` 必须全绿;同时回归 `tests/test_web_assets.py`(14 项)不受影响。

## 4. Out of Scope

- 不动 `docs/`、源码、前端任何文件;
- 不做 git commit / push(由主会话评审后提交);
- 不处理 git 历史重写(历史 blob 的乱码保留原样);
- 不重构 README 内容结构(只修编码与不可逆字符,保持现有章节布局)。

## 5. 验收清单

- [ ] 两个 README strict UTF-8 解码通过、无 BOM、无 PUA/U+FFFD;
- [ ] 中文内容通顺,占位符零残留;表格/代码块结构与修复前一致;
- [ ] Bailian 相关行(v2.2 新增)仍在且正确;
- [ ] `tests/test_docs_encoding.py` 新建且通过;相关回归通过;
- [ ] 最终报告列出:重建的不确定措辞清单、文档与代码不符的修正点(如有)。
