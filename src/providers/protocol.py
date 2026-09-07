"""Provider protocol types for Buddy2api 2.0 channel isolation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

ChannelId = Literal["workbuddy", "qclaw", "qwenwork", "qoderwork", "traework", "traesolo", "gmi"]

KNOWN_CHANNEL_IDS: tuple[ChannelId, ...] = (
    "workbuddy",
    "qclaw",
    "qwenwork",
    "qoderwork",
    "traework",
    "traesolo",
    "gmi",
)
KNOWN_CHANNEL_SET = frozenset(KNOWN_CHANNEL_IDS)


class CheckinUnsupported(Exception):
    """Channel has no daily check-in claim API."""


class ChannelUnavailable(Exception):
    def __init__(self, channel: str, message: str = ""):
        self.channel = channel
        super().__init__(message or f"No usable accounts for channel '{channel}'")


class UnknownChannel(Exception):
    def __init__(self, channel: str, message: str = ""):
        self.channel = channel
        super().__init__(message or f"Unknown or disabled channel '{channel}'")


class InvalidModel(Exception):
    def __init__(self, model: str, message: str = ""):
        self.model = model
        super().__init__(message or f"Invalid model '{model}'")


class UnknownModel(Exception):
    def __init__(self, model: str, message: str = ""):
        self.model = model
        super().__init__(message or f"Unknown model '{model}'")


class KeyChannelMismatch(Exception):
    def __init__(self, channel: str, key_channel: str, message: str = ""):
        self.channel = channel
        self.key_channel = key_channel
        super().__init__(
            message
            or f"API key is bound to '{key_channel}', not '{channel}'"
        )


@dataclass(frozen=True)
class BindResult:
    channel: ChannelId
    inner: str
    original: str


@dataclass(frozen=True)
class DiscoveredFile:
    channel: ChannelId
    path: str
    valid: bool
    reason: str
    account_name: str
    uid_masked: str
    already_imported: bool
    extra_preview: dict = field(default_factory=dict)


@dataclass
class DiscoverResult:
    dirs: list[dict]
    files: list[DiscoveredFile]
    file_count: int
    valid_count: int
    importable_count: int
    preview_token: str = ""


@dataclass
class QuotaSnapshot:
    ok: bool
    channel: ChannelId
    account_id: int
    unit: str
    remaining: float | None
    extra: dict = field(default_factory=dict)
    unsupported: bool = False
    message: str = ""


@runtime_checkable
class Provider(Protocol):
    """五家 provider 的公共核(35号方案 §2.3:9 方法五家齐备)。

    仅注解用:禁止在业务路径做 isinstance/运行时强制——各家的签名差异
    (如 LoginCapable 两家形态不同)由能力 Protocol 与契约测试钉住。
    """

    id: ChannelId
    display_name: str
    checkin_supported: bool

    def list_models(self) -> list[dict]: ...

    def alias_map(self) -> dict[str, str]: ...

    def accepts_model(self, inner: str) -> bool: ...

    def translate_model(self, model: str) -> str: ...

    def pick_account(self, exclude_ids: set[int] | None = None) -> dict | None: ...

    async def pick_account_with_fallback(
        self, exclude_ids: set[int] | None = None
    ) -> dict | None: ...

    async def has_usable_account(self) -> bool: ...

    async def chat_completions(
        self, payload: dict, api_key_info: dict | None
    ) -> tuple: ...

    def fetch_model_rates(self) -> list[dict]: ...


@runtime_checkable
class StoreCapable(Protocol):
    """本地凭据目录发现/导入(workbuddy 除外,4/5 家)。"""

    def discover(self) -> dict: ...

    def import_path(self, path: str) -> dict: ...

    def parse_credentials(self, body: dict) -> dict: ...


@runtime_checkable
class RefreshCapable(Protocol):
    """账号级 token 刷新(qclaw/qwenwork/traework/traesolo)。"""

    async def refresh(self, account: dict) -> dict: ...


@runtime_checkable
class TestChatCapable(Protocol):
    """单账号探活(workbuddy 走 proxy.test_account_chat 模块函数,4/5 家)。"""

    async def test_chat(self, account: dict, model: str = "auto", prompt: str = "ping") -> dict: ...


@runtime_checkable
class QuotaCapable(Protocol):
    """官方额度查询(fetch_quota -> QuotaSnapshot;workbuddy 无独立额度 API)。"""

    async def fetch_quota(self, account: dict) -> QuotaSnapshot: ...


@runtime_checkable
class UpsertCapable(Protocol):
    """粘贴凭据直接建号(qwenwork/traework/traesolo;qclaw 走 import_path)。"""

    def upsert_account(self, parsed: dict) -> dict: ...


@runtime_checkable
class CheckinCapable(Protocol):
    """每日签到(traework/traesolo)。"""

    async def fetch_checkin(self, account: dict) -> dict: ...

    async def claim_checkin(self, account: dict) -> dict: ...


@runtime_checkable
class QclawLoginCapable(Protocol):
    """qclaw 扫码登录流(异步双函数)。"""

    async def start_login(self) -> dict: ...

    async def complete_login(self, data: dict) -> dict: ...


@runtime_checkable
class SoloLoginCapable(Protocol):
    """traesolo SOLO 登录流(同步三函数 + 异步回调)。"""

    def start_login(self) -> dict: ...

    def login_result(self) -> dict: ...

    def cancel_login(self) -> dict: ...

    async def complete_login_callback(self, request: dict) -> dict: ...


@runtime_checkable
class DynamicModelsCapable(Protocol):
    """官方模型目录动态拉取(仅 traesolo)。"""

    async def refresh_dynamic_models(self, force: bool = False) -> bool: ...
