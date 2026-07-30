from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CORS_ORIGINS = (
    "http://localhost:8081",
    "http://127.0.0.1:8081",
    "http://localhost:19006",
    "http://127.0.0.1:19006",
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
    max_file_bytes: int = 20 * 1024 * 1024
    scheduler_poll_seconds: int = 60
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS

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
