from __future__ import annotations

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
