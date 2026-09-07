# 前端手测清单(10 分钟)

> 依据 31 号优化方案 §5 验收要求落成文档;每次改动 `src/web/**` 后过一遍,
> PR 描述里引用。全部为浏览器侧手工验证,零构建零依赖。
> 启动:`.venv/Scripts/python.exe server.py`(或项目常规启动方式),浏览器打开
> `http://127.0.0.1:8787/`。静态防退化由 `tests/test_web_assets.py` 兜底。

## 1. 错误文案与 toast(约 2 分钟)

- [ ] 制造一次 401(设置页点「清除」凭证后再刷新数据):错误 toast 文案为「管理凭证无效或已过期…」,尾部出现「去设置」按钮
- [ ] 点「去设置」:toast 关闭并跳到设置页
- [ ] 错误 toast 停留约 8 秒不自动消失,右上角有「×」关闭按钮,点击立即消失
- [ ] 成功操作(如切换 Key 通道)toast 约 2.5 秒自动消失,无「×」按钮
- [ ] DevTools 断网(或停服务)刷新:503/504 文案含「下一步动作」(稍候重试/检查服务进程),不再是裸的「加载失败」

## 2. 无障碍:aria-live 与键盘可达(约 2 分钟)

- [ ] DevTools 检查 `.toasts` 容器带 `role="status"` 与 `aria-live="polite"`
- [ ] Tab 键从页面顶部出发,左侧 rail 每个导航项可聚焦,聚焦有描边(focus-visible)
- [ ] 聚焦 rail 项按 Enter 或 Space:与鼠标点击同义,页面切换
- [ ] 通道管理页:Tab 到任一通道行,按 Enter/Space 行选中(详情浮窗数据随之切换);行内按钮聚焦时按 Enter 只触发按钮本身,不误触行选中
- [ ] API Keys 页:列表 Key 列默认显示掩码(key_prefix),点「查看」出现明文,点「隐藏」恢复掩码

## 3. 写操作无整页重载 / 局部更新(约 2 分钟)

- [ ] 通道管理页:账号「启用/禁用」「保存」后表格不闪烁(无整表 spinner),该行状态就地更新
- [ ] API Keys 页:切换通道 / 禁用启用后行就地更新,不出现整表 spinner
- [ ] 模型配置页:保存白名单后不触发其它通道面板的重新拉取(Network 面板仅一个 PUT),生效列表就地回写
- [ ] 额度页点「刷新官方额度」:Network 面板只发一次 `POST /admin/accounts/resources/batch`(不再是 N 个 GET),各行余额就地更新
- [ ] (回退保底)临时把后端批量端点改 404 或断网:批量刷新回退为逐个请求并 toast 提示,页面不报错白屏

## 4. 生命周期与去重(约 2 分钟)

- [ ] 通道管理页 → 其它页 → 返回通道管理页:拖拽排序仍可用(Sortable 二次进页不失效)
- [ ] 通道管理页发起 SOLO 网页登录(等待轮询中)切到其它页:Network 面板 2.5s 周期的 login/result 轮询停止(无残留请求)
- [ ] 双击任意「刷新」按钮:Network 面板同一 GET 只发一次(in-flight 去重)
- [ ] Network 面板观察任一挂起请求:默认 15s 超时;账号「测试」等慢操作不会被 15s 掐断(调用方已放宽)

## 5. 首屏与 hash 路由(约 2 分钟)

- [ ] Network 面板:vue/Sortable 两个 vendor script 带 defer,字体 woff2 有 7 条 preload;刷新页面无字体闪烁(FOUT)
- [ ] 切到「API Keys」页后刷新浏览器:直接还原到 API Keys(hash 为 `#/keys`)
- [ ] 手动把地址改成 `#/logs` 回车:页面切换到请求日志
- [ ] 清掉 hash(空)刷新:回退到 localStorage 记忆页(cb_gw_page_v2),行为与旧版一致

## 备注

- 「空库向导」(31 方案 P2-10)本轮暂缓,不在本清单。
- 静态 smoke 对应 `tests/test_web_assets.py`:错误映射表、api.js 去重/超时、
  index.html defer/preload/aria-live、Sortable 幂等守卫、keys reveal、hash 路由、
  键盘可达、局部更新 helper、quota 批量端点。
