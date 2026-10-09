"""MiniMax Code —「客户端活性门」：网关要不要主动打 OAuth 刷新，先看桌面端开没开。

背景（spec:694 + `.tmp/mitm/minimax-code-20260919/ROTATION-VERDICT.md`）：
网关与 MiniMax Code 桌面客户端**共用同一份 OAuth 凭据**，而 refresh_token 是
「一次刷新一轮转」的：客户端约每 1h 自刷一次、每次都换 refresh_token ⇒ 网关若在
客户端**正要**刷新时抢先刷了，就会把客户端手里的票作废（客户端侧还有 auth.lock
串行，网关在锁外，绕不过去，spec:279,315）。已经落地的对策是「刷新前先从磁盘接管」
（`store.adopt_credentials_from_client(..., require_newer=True)`），本模块补上另一半：
**别在客户端活着的时候主动刷**。

三态语义（环境变量 `CB_MINIMAX_CODE_GATEWAY_SELF_REFRESH`，键名与默认值登记在
`constants.py`，这里只读不重复定义）：
  * `auto`（缺省）—— 由进程探测决定：客户端进程**不在**才允许网关自刷；在 ⇒ 让位，
    把刷新权交给客户端，网关下一次对齐/自愈时从磁盘接管新票。
  * `on`  —— 总是允许自刷（认定网关是唯一持有者，或运维明确接受顶掉客户端）。
  * `off` —— 从不允许自刷（纯被动：只靠接管客户端落盘的凭据活着的部署）。
  缺省值与任何非法取值一律归约为 `auto`：宁可按探测走，也不因配置笔误锁死行为。

auto 的**降级方向**（刻意的，不是偷懒）：探测函数任何失败（非 Windows、ctypes 不可用、
Toolhelp 调用出错、沙箱看不见其它用户态进程）都判为「客户端没开」⇒ **允许**自刷。
理由是两种误判的代价不对称：误判「客户端开着」会让网关彻底失去自刷新能力（被探测
bug 弄瞎，越放越久直到 access token 到期，网关整体不可用）；误判「客户端没开」只是
退回改造前的既有行为，且仍受磁盘接管兜着。

红线（与 spec:694 一致）：本模块**只读本地进程表**——不读写客户端文件
（auth.json / auth-state.json 都不碰）、不发任何对 MiniMax 生产的网络请求、
不碰 DB。日志与返回文案只含模式枚举值、进程名匹配子串（配置项本身）与决策结果，
绝无 token/secret/凭证原文。

测试注入点：模块级 `_PROBE`（默认绑定 `_default_probe`）——替换它即可绕开真实进程
扫描（跨平台、无权限依赖）；匹配子串走 `CB_MINIMAX_CODE_CLIENT_PROCESS_MATCH`
或 `setattr(liveness, "SUBSTRING", ...)`。
"""

from __future__ import annotations

import logging
import os

# 键名与默认值取自 constants（契约层）；getattr 兜底防契约层改名把这里打断，
# 与 token.py 的 `getattr(_C, ...)` 同款写法。
from providers.minimax_code import constants as K

logger = logging.getLogger(__name__)

ENV_GATEWAY_SELF_REFRESH = str(getattr(K, "ENV_GATEWAY_SELF_REFRESH", "") or "") or (
    "CB_MINIMAX_CODE_GATEWAY_SELF_REFRESH"
)
ENV_CLIENT_PROCESS_MATCH = str(getattr(K, "ENV_CLIENT_PROCESS_MATCH", "") or "") or (
    "CB_MINIMAX_CODE_CLIENT_PROCESS_MATCH"
)

# 自刷模式的唯一合法取值。非法/缺省 ⇒ AUTO（见模块 docstring）。
MODE_AUTO = "auto"
MODE_ON = "on"
MODE_OFF = "off"
MODE_VALUES = (MODE_AUTO, MODE_ON, MODE_OFF)
_MODE_DEFAULT = str(getattr(K, "GATEWAY_SELF_REFRESH_MODE_DEFAULT", "") or "") or MODE_AUTO
_MATCH_DEFAULT = str(getattr(K, "CLIENT_PROCESS_MATCH_DEFAULT", "") or "") or "minimax code"

# 进程名匹配子串的**回落值**（constants 的默认值，导入期取一次）。真正生效的值由
# `client_process_match()` 每次现读环境变量、未设置/空串时回落到这里 ⇒ 运维改
# `CB_MINIMAX_CODE_CLIENT_PROCESS_MATCH` 不必重启；测试两种改法都认：
# `setenv(ENV_CLIENT_PROCESS_MATCH, ...)`（现读）或 `setattr(liveness, "SUBSTRING", ...)`。
SUBSTRING = _MATCH_DEFAULT

_MAX_PATH = 260  # Win32 MAX_PATH；PROCESSENTRY32W.szExeFile 的数组长度
_TH32CS_SNAPPROCESS = 0x00000002  # Toolhelp 快照标志：枚举进程


def _env_str(name: str, default: str) -> str:
    """环境变量覆盖（repo 惯例 CB_* 前缀）；未设置/空串回退默认值。每次调用现读。"""
    raw = str(os.environ.get(name) or "").strip()
    return raw or default


def self_refresh_mode() -> str:
    """返回 `auto`/`on`/`off`；非法取值一律归约为 `auto`（绝不为笔误改变行为极性）。"""
    value = _env_str(ENV_GATEWAY_SELF_REFRESH, _MODE_DEFAULT).lower()
    return value if value in MODE_VALUES else MODE_AUTO


def client_process_match() -> str:
    """当前生效的进程名匹配子串（大小写不敏感语义由探测侧 lower() 保证）。"""
    return _env_str(ENV_CLIENT_PROCESS_MATCH, SUBSTRING)


def _default_probe(substring: str) -> bool:
    """扫描本机进程表：任一进程名（lower 后）**包含** `substring` 即判定客户端在运行。

    用 kernel32 的 Toolhelp32 快照（CreateToolhelp32Snapshot + Process32First/Next），
    只读 PROCESSENTRY32W.szExeFile 里的进程名：不读命令行、不开进程句柄（只需名字）。
    Windows 专属 ⇒ 非 Windows 直接 False（这里判的是"探测不出"，由 `is_client_running`
    的 fail-open 语义消化）。命中即提前返回 True，快照句柄由 finally 关掉。
    任何异常都原样外抛，由 `is_client_running` 统一兜成 False——保持单一降级点。

    ⚠️ 已知观测限制：在受限执行环境（如 DSH 沙箱）里其它用户态进程可能不可见，
    扫描到 0 命中属正常，不代表本实现有误；降级路径（fail-open）正是为这种情况准备的。
    """
    if os.name != "nt":
        return False
    needle = str(substring or "").strip().lower()
    if not needle:
        # 空子串会命中所有进程，那等于"永远认为客户端在开"⇒ 反而把网关自刷能力锁死。
        # 视为"探测素材不足"，与探测失败同极（fail-open）。
        return False

    import ctypes
    from ctypes import wintypes

    class _ProcessEntry32W(ctypes.Structure):
        # 严格对齐 Win32 PROCESSENTRY32W（th32DefaultHeapID 是 ULONG_PTR ⇒ c_size_t，
        # 64 位下少这一项会把 szExeFile 的偏移整体挪错）。
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("th32Threads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * _MAX_PATH),
        ]

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot
    snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    snapshot.restype = wintypes.HANDLE
    first = kernel32.Process32FirstW
    nxt = kernel32.Process32NextW
    for fn in (first, nxt):
        fn.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        fn.restype = wintypes.BOOL
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    handle = snapshot(_TH32CS_SNAPPROCESS, 0)
    invalid = ctypes.c_void_p(-1).value  # INVALID_HANDLE_VALUE（按位宽取值，别写死）
    if handle is None or int(handle) == int(invalid or -1):
        return False
    entry = _ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
    try:
        ok = first(handle, ctypes.byref(entry))
        while ok:
            if needle in str(entry.szExeFile or "").lower():
                return True
            ok = nxt(handle, ctypes.byref(entry))
        return False
    finally:
        close(handle)


_PROBE = _default_probe  # 测试注入点：`monkeypatch.setattr(liveness, "_PROBE", lambda sub: ...)`


def is_client_running() -> bool:
    """桌面客户端进程是否在运行；**best-effort**，任何异常/非 Windows ⇒ False（fail-open）。

    判 False 的含义是"探测不出客户端在跑"⇒ 门控放行自刷（降级方向见模块 docstring）。
    探测结果只用于是否**主动**刷新，不影响从磁盘接管（后者任何时候都可以做）。
    """
    try:
        return bool(_PROBE(client_process_match()))
    except Exception as exc:  # noqa: BLE001 - 活性探测失败绝不能打断刷新链路
        logger.debug("[minimax-code-liveness] 进程探测异常，按「客户端未运行」处理：%r", exc)
        return False


def _decide(mode: str, client_running: bool) -> bool:
    """三态 + 探测结果 ⇒ 单个布尔（`allow_gateway_self_refresh` 与 `startup_note` 共用，防两份判定漂移）。"""
    if mode == MODE_ON:
        return True
    if mode == MODE_OFF:
        return False
    return not client_running  # auto（含非法值归约后的 auto）


def allow_gateway_self_refresh() -> bool:
    """网关此刻是否允许**主动**打 OAuth 刷新（token.py 的主动预刷路径据此放行/让位）。

    True  ⇒ 可以主动刷；False ⇒ 让位给客户端，只走磁盘接管。
    被动路径（401 → 刷新 → 单次重放，spec:313）不归本门控：那是已经失败了，
    不刷就只能报错，调用方自己决定要不要先试接管。
    """
    mode = self_refresh_mode()
    # on/off 是显式指令，不需要探测 ⇒ 只在 auto 时才扫进程表（少一次无谓的 ctypes 调用）。
    return _decide(mode, is_client_running() if mode == MODE_AUTO else False)


def startup_note() -> str:
    """给运维看的一句中文摘要（启动/状态面用）：模式 + 净决策 + 让位/放行的原因。

    只含模式枚举值与进程名匹配子串（配置项本身）⇒ 不含任何凭证原文。

    精简说明：原实现把同一个 _decide() 结论说了两遍（前半句「主动刷新允许」与
    后半句 why 的结论重复），且 why 在 auto 下又复述了一遍前面已给出的
    mode + 探测结果，一行 118 字符。现在每个事实只出现一次。
    探测结论用「未检测到」而非「客户端没开」：前者是一律归约后的结果
    （进程表不可读 / 非 Windows / 探针异常都落这里，见 is_client_running），
    照实表述，不把探测失败讲成确定事实。
    """
    mode = self_refresh_mode()
    running = is_client_running()
    allowed = _decide(mode, running)
    if mode == MODE_ON:
        why = "显式 on，无视进程探测"
    elif mode == MODE_OFF:
        why = "显式 off，永不主动刷"
    elif running:
        why = "检测到客户端在运行，让位给客户端"
    else:
        why = "未检测到客户端（含探测不可用）"
    verdict = "允许" if allowed else "禁止"
    # 匹配子串只在 auto 下才有意义（on/off 是显式指令，不看进程表）；
    # 它也是 CB_MINIMAX_CODE_CLIENT_PROCESS_MATCH 是否覆盖生效的唯一可见处。
    suffix = f"；匹配子串 {client_process_match()!r}" if mode == MODE_AUTO else ""
    return f"[minimax-code] 自刷新活性门：mode={mode}，主动刷新{verdict}（{why}）{suffix}"
