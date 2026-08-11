from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CORS_ORIGINS = (
    "http://localhost:8081",
    "http://127.0.0.1:8081",
    "http://localhost:19006",
    "http://127.0.0.1:19006",
    "http://127.0.0.1:17818",
)


def _default_data_root() -> Path:
    xdg_data_home = os.getenv("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home).expanduser() / "centaurai-pocket"
    return Path.home() / ".local" / "share" / "centaurai-pocket"


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings with intentionally separate product identity and storage."""

    data_root: Path
    host: str = "127.0.0.1"
    port: int = 8718
    owner_token: str | None = None
    agent_token: str | None = None
    desktop_session_token: str | None = None
    max_file_bytes: int = 20 * 1024 * 1024
    scheduler_poll_seconds: int = 60
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS
    task_execution_public_origin: str | None = None
    outlook_client_id: str | None = None
    outlook_tenant: str = "common"
    # 秘书 Agent 模型编排（§3.4）：编排在服务端，手机端绝不持有模型密钥
    assistant_provider: str | None = None
    assistant_model: str = "qwen2.5:14b"
    ollama_url: str = "http://127.0.0.1:11434"
    assistant_cloud_provider: str | None = None
    assistant_cloud_model: str = "claude-opus-5"
    assistant_cloud_api_key: str | None = None
    assistant_cloud_base_url: str = "https://api.anthropic.com"

    def __post_init__(self) -> None:
        value = self.task_execution_public_origin
        if value is None:
            return
        if not isinstance(value, str):
            raise TypeError("task execution browser origin 必须是字符串")
        if not value.strip():
            object.__setattr__(self, "task_execution_public_origin", None)
            return
        # Keep the runtime setting and browser BFF on one canonical-origin
        # implementation without making config import the browser stack until
        # the feature is explicitly enabled.
        from .workspace.task_execution_browser import _validate_origin

        object.__setattr__(
            self,
            "task_execution_public_origin",
            _validate_origin(value),
        )

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_root=Path(
                os.getenv("CENTAURAI_POCKET_DATA_DIR") or str(_default_data_root())
            ).expanduser(),
            host=os.getenv("CENTAURAI_POCKET_HOST", "127.0.0.1"),
            port=int(os.getenv("CENTAURAI_POCKET_PORT", "8718")),
            owner_token=os.getenv("CENTAURAI_POCKET_OWNER_TOKEN") or None,
            agent_token=os.getenv("CENTAURAI_POCKET_AGENT_TOKEN") or None,
            desktop_session_token=(
                os.getenv("CENTAURAI_POCKET_DESKTOP_SESSION_TOKEN") or None
            ),
            max_file_bytes=int(
                os.getenv("CENTAURAI_POCKET_MAX_FILE_BYTES", str(20 * 1024 * 1024))
            ),
            scheduler_poll_seconds=int(
                os.getenv("CENTAURAI_POCKET_SCHEDULER_POLL_SECONDS", "60")
            ),
            cors_origins=tuple(
                origin.strip().rstrip("/")
                for origin in os.getenv(
                    "CENTAURAI_POCKET_CORS_ORIGINS",
                    ",".join(DEFAULT_CORS_ORIGINS),
                ).split(",")
                if origin.strip()
            ),
            task_execution_public_origin=os.getenv(
                "CENTAURAI_POCKET_TASK_EXECUTION_PUBLIC_ORIGIN"
            ),
            outlook_client_id=(
                os.getenv("CENTAURAI_POCKET_OUTLOOK_CLIENT_ID", "").strip() or None
            ),
            outlook_tenant=(
                os.getenv("CENTAURAI_POCKET_OUTLOOK_TENANT", "common").strip()
                or "common"
            ),
            assistant_provider=(
                os.getenv("CENTAURAI_POCKET_ASSISTANT_PROVIDER", "").strip() or None
            ),
            assistant_model=(
                os.getenv("CENTAURAI_POCKET_ASSISTANT_MODEL", "").strip()
                or "qwen2.5:14b"
            ),
            ollama_url=(
                os.getenv("CENTAURAI_POCKET_OLLAMA_URL", "").strip()
                or "http://127.0.0.1:11434"
            ),
            assistant_cloud_provider=(
                os.getenv("CENTAURAI_POCKET_ASSISTANT_CLOUD_PROVIDER", "").strip()
                or None
            ),
            assistant_cloud_model=(
                os.getenv("CENTAURAI_POCKET_ASSISTANT_CLOUD_MODEL", "").strip()
                or "claude-opus-5"
            ),
            assistant_cloud_api_key=(
                os.getenv("CENTAURAI_POCKET_ASSISTANT_CLOUD_API_KEY", "").strip()
                or None
            ),
            assistant_cloud_base_url=(
                os.getenv("CENTAURAI_POCKET_ASSISTANT_CLOUD_BASE_URL", "").strip()
                or "https://api.anthropic.com"
            ),
        )

    @property
    def database_path(self) -> Path:
        return self.data_root / "pocket.db"

    @property
    def agent_token_path(self) -> Path:
        return self.data_root / "agent-token"

    @property
    def owner_token_path(self) -> Path:
        return self.data_root / "owner-token"

    @property
    def task_session_hmac_key_path(self) -> Path:
        return self.data_root / "task-session-hmac-key"

    def prepare(self) -> tuple[str, str]:
        """Create private paths and return the Owner and Agent credentials."""

        self.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.data_root.chmod(0o700)
        except OSError:
            pass

        owner_token = self._resolve_token(
            configured=self.owner_token,
            path=self.owner_token_path,
            prefix="cp_owner_",
        )
        agent_token = self._resolve_token(
            configured=self.agent_token,
            path=self.agent_token_path,
            prefix="cp_live_",
        )
        if secrets.compare_digest(owner_token, agent_token):
            raise ValueError("Owner token 与 Agent token 必须使用不同的值")
        if self.desktop_session_token:
            if not self.desktop_session_token.startswith("cp_desktop_"):
                raise ValueError("桌面会话 token 格式无效")
            if any(
                secrets.compare_digest(self.desktop_session_token, candidate)
                for candidate in (owner_token, agent_token)
            ):
                raise ValueError("桌面会话 token 必须与长期凭据不同")
        return owner_token, agent_token

    def rotate_agent_token(self) -> str:
        """Replace the generated Agent token and return it exactly once."""

        if self.agent_token is not None:
            raise ValueError(
                "Agent token 由环境变量管理；请修改环境变量并重启服务"
            )
        token = f"cp_live_{secrets.token_urlsafe(32)}"
        self.agent_token_path.write_text(f"{token}\n", encoding="utf-8")
        try:
            self.agent_token_path.chmod(0o600)
        except OSError:
            pass
        return token

    def resolve_task_session_hmac_key(self, legacy_key: bytes) -> bytes:
        """Persist the legacy-derived task key and decouple future Owner rotation."""

        if not isinstance(legacy_key, bytes) or len(legacy_key) != 32:
            raise ValueError("任务会话 HMAC 初始 key 必须是 32 字节")
        path = self.task_session_hmac_key_path
        if path.is_symlink():
            raise ValueError("任务会话 HMAC key 文件不能是符号链接")
        if not path.exists():
            temporary_path = path.with_name(
                f".{path.name}.{secrets.token_hex(16)}.tmp"
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor: int | None = None
            published = False
            try:
                descriptor = os.open(temporary_path, flags, 0o600)
                encoded_key = f"{legacy_key.hex()}\n".encode("ascii")
                written = 0
                while written < len(encoded_key):
                    count = os.write(descriptor, encoded_key[written:])
                    if count <= 0:
                        raise OSError("任务会话 HMAC key 写入未完成")
                    written += count
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                try:
                    os.link(
                        temporary_path,
                        path,
                        follow_symlinks=False,
                    )
                    published = True
                except FileExistsError:
                    # Another process won the first-start race.  The winner is
                    # validated below; the fully written temporary inode is
                    # never exposed as the final key file.
                    pass
                if published:
                    directory_flags = os.O_RDONLY
                    if hasattr(os, "O_DIRECTORY"):
                        directory_flags |= os.O_DIRECTORY
                    directory_descriptor = os.open(path.parent, directory_flags)
                    try:
                        os.fsync(directory_descriptor)
                    finally:
                        os.close(directory_descriptor)
            except OSError as error:
                raise ValueError("无法持久化任务会话 HMAC key 文件") from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise ValueError("无法清理任务会话 HMAC key 临时文件") from error
        try:
            metadata = path.lstat()
        except OSError as error:
            raise ValueError("无法读取任务会话 HMAC key 文件") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("任务会话 HMAC key 必须是普通文件")
        try:
            path.chmod(0o600)
            encoded = path.read_text(encoding="ascii").strip()
            key = bytes.fromhex(encoded)
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError("任务会话 HMAC key 文件格式无效") from error
        if len(encoded) != 64 or len(key) != 32 or key.hex() != encoded.lower():
            raise ValueError("任务会话 HMAC key 文件格式无效")
        return key

    @staticmethod
    def _resolve_token(*, configured: str | None, path: Path, prefix: str) -> str:
        if configured:
            return configured

        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
                return token

        token = f"{prefix}{secrets.token_urlsafe(32)}"
        path.write_text(f"{token}\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return token
