from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from centaur_pocket.config import Settings


def test_prepare_generates_separate_private_tokens(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path / "runtime")
    owner_token, agent_token = settings.prepare()

    assert owner_token.startswith("cp_owner_")
    assert agent_token.startswith("cp_live_")
    assert owner_token != agent_token
    assert settings.owner_token_path.read_text(encoding="utf-8").strip() == owner_token
    assert settings.agent_token_path.read_text(encoding="utf-8").strip() == agent_token
    assert stat.S_IMODE(settings.owner_token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.agent_token_path.stat().st_mode) == 0o600

    assert settings.prepare() == (owner_token, agent_token)


def test_prepare_rejects_shared_owner_and_agent_token(tmp_path: Path) -> None:
    settings = Settings(
        data_root=tmp_path / "runtime",
        owner_token="same-secret",
        agent_token="same-secret",
    )

    with pytest.raises(ValueError, match="必须使用不同"):
        settings.prepare()


def test_environment_managed_agent_token_cannot_rotate(tmp_path: Path) -> None:
    settings = Settings(
        data_root=tmp_path / "runtime",
        agent_token="cp_live_environment",
    )
    settings.prepare()

    with pytest.raises(ValueError, match="环境变量管理"):
        settings.rotate_agent_token()


def test_empty_data_directory_environment_uses_private_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("CENTAURAI_POCKET_DATA_DIR", "")

    settings = Settings.from_env()

    assert settings.data_root == tmp_path / "xdg-data" / "centaurai-pocket"


def test_task_session_hmac_key_is_persistent_private_and_owner_independent(
    tmp_path: Path,
) -> None:
    settings = Settings(data_root=tmp_path / "runtime")
    settings.prepare()
    legacy_key = b"a" * 32

    assert settings.resolve_task_session_hmac_key(legacy_key) == legacy_key
    assert stat.S_IMODE(settings.task_session_hmac_key_path.stat().st_mode) == 0o600
    assert settings.task_session_hmac_key_path.read_text(encoding="ascii") == (
        f"{legacy_key.hex()}\n"
    )

    # Owner rotation changes the legacy-derived candidate, but never an
    # already-published task-session signing key.
    assert settings.resolve_task_session_hmac_key(b"b" * 32) == legacy_key


def test_task_session_hmac_key_partial_write_is_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_root=tmp_path / "runtime")
    settings.prepare()
    original_write = os.write
    call_count = 0

    def interrupted_write(descriptor: int, content: bytes) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return original_write(descriptor, content[:8])
        raise OSError("simulated power loss")

    monkeypatch.setattr(os, "write", interrupted_write)
    with pytest.raises(ValueError, match="无法持久化"):
        settings.resolve_task_session_hmac_key(b"c" * 32)

    assert not settings.task_session_hmac_key_path.exists()
    assert list(settings.data_root.glob(".task-session-hmac-key.*.tmp")) == []

    monkeypatch.setattr(os, "write", original_write)
    assert settings.resolve_task_session_hmac_key(b"c" * 32) == b"c" * 32


def test_task_execution_public_origin_is_explicit_and_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled = Settings(
        data_root=tmp_path / "disabled",
        task_execution_public_origin="",
    )
    whitespace_disabled = Settings(
        data_root=tmp_path / "whitespace-disabled",
        task_execution_public_origin="   ",
    )
    canonical = Settings(
        data_root=tmp_path / "canonical",
        task_execution_public_origin="https://tasks.example.test/",
    )

    assert disabled.task_execution_public_origin is None
    assert whitespace_disabled.task_execution_public_origin is None
    assert canonical.task_execution_public_origin == "https://tasks.example.test"

    monkeypatch.setenv("CENTAURAI_POCKET_TASK_EXECUTION_PUBLIC_ORIGIN", "")
    assert Settings.from_env().task_execution_public_origin is None
    monkeypatch.setenv(
        "CENTAURAI_POCKET_TASK_EXECUTION_PUBLIC_ORIGIN",
        "https://tasks.example.test/",
    )
    assert (
        Settings.from_env().task_execution_public_origin
        == "https://tasks.example.test"
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://tasks.example.test",
        "https://user@tasks.example.test",
        "https://tasks.example.test:443",
        "https://tasks.example.test/path",
        "https://tasks.example.test?source=test",
        "https://tasks.example.test#fragment",
        "https://tasks.example.test:invalid",
        " https://tasks.example.test",
    ],
)
def test_task_execution_public_origin_rejects_noncanonical_values(
    tmp_path: Path,
    origin: str,
) -> None:
    with pytest.raises(ValueError):
        Settings(
            data_root=tmp_path / "invalid-origin",
            task_execution_public_origin=origin,
        )
