# GMI pick_account_with_fallback 缺 await · 修复 spec (EXECUTION SPEC)

> 本文件是**自包含、可直接由 subagent 执行**的实施规范。subagent **不需要回看主对话**。
> 修复一个 RuntimeWarning:`coroutine 'pick_account_with_fallback' was never awaited`。
> 红线:中文零 em-dash;只 `git add` 明确文件。

---

## 0. Bug 根因

`src/providers/gmi/__init__.py` 的 `GmiProvider.pick_account_with_fallback`(约 55-61 行)内部调用 `auth_manager.pick_account_with_fallback(...)` 时**缺 `await`**:

```python
async def pick_account_with_fallback(self, exclude_ids=None) -> Optional[dict]:
    from accounts import auth_manager
    store.ensure_env_account()
    return auth_manager.pick_account_with_fallback(set(exclude_ids or ()), provider=CHANNEL_ID)  # ← 缺 await!
```

`auth_manager.pick_account_with_fallback` 是 **async 函数**(`src/accounts/auth_manager.py:966`),不 await 会返回未执行的 coroutine。导致:
- `has_usable_account()` 里 `await self.pick_account_with_fallback() is not None` 恒为 True(拿到的是 coroutine 对象)
- coroutine 从未执行 → RuntimeWarning

**只有 GMI 有此 bug**。其他 provider(workbuddy/qclaw/qwenwork/traesolo/traework)都正确(有 await)。

## 1. 修复(两个仓库)

### 1.1 主仓库 `C:\Usr\Code\etc\Buddy2api`(分支 main)

文件:`src/providers/gmi/__init__.py` 第 61 行
```python
# 改前
return auth_manager.pick_account_with_fallback(set(exclude_ids or ()), provider=CHANNEL_ID)
# 改后
return await auth_manager.pick_account_with_fallback(set(exclude_ids or ()), provider=CHANNEL_ID)
```

### 1.2 prod 仓库 `C:\Usr\Code\etc\Buddy2api-prod`(分支 prod)

同一文件 `src/providers/gmi/__init__.py` 同一行,同样修复。

## 2. 验证

```bash
# 主仓库
cd C:\Usr\Code\etc\Buddy2api
python -c "import ast; ast.parse(open('src/providers/gmi/__init__.py',encoding='utf-8').read()); print('syntax ok')"
python -m pytest -q tests/test_gmi_store.py tests/test_web_assets.py 2>&1 | Select-Object -Last 3
```

> 若有 gmi 相关测试,跑一下;没有则语法检查 + 现有测试即可。

## 3. Commit + push

### 主仓库
```bash
cd C:\Usr\Code\etc\Buddy2api
git add src/providers/gmi/__init__.py
git commit -m "fix(gmi): await pick_account_with_fallback in provider wrapper"
git push origin main
```

### prod 仓库
```bash
cd C:\Usr\Code\etc\Buddy2api-prod
git add src/providers/gmi/__init__.py
git commit -m "fix(gmi): await pick_account_with_fallback in provider wrapper"
git push origin prod
```

## 4. 交付后自检

- [ ] 主仓库 `git status --short` 干净
- [ ] prod 仓库 `git status --short` 干净
- [ ] `git grep -n "return auth_manager.pick_account_with_fallback" -- src/providers/gmi/__init__.py` → 0(已加 await)
- [ ] 两个仓库都已 push

## 5. 兜底

- 若 prod 仓库有未提交改动(部署残留),先 `git status` 确认,只 add gmi 文件,不要动其他。
- 若 prod 的 gmi 文件与 spec 行号不符,按符号名 `pick_account_with_fallback` grep 定位。
- 中文 UI 文案零 em-dash;英文 commit/注释可含。
