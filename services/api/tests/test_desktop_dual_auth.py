from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from centaur_pocket.config import Settings
from centaur_pocket.main import create_app


def _owner_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Owner-Token": token,
    }


def _assert_owner_access(client: TestClient, token: str, expected: int) -> None:
    headers = _owner_headers(token)
    assert client.get("/api/v1/dashboard", headers=headers).status_code == expected
    assert (
        client.get(
            "/api/v1/workspaces/ws_default/bootstrap", headers=headers
        ).status_code
        == expected
    )


def test_desktop_sidecar_accepts_stable_and_session_owner_tokens(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "runtime"
    session_token = "cp_desktop_session-a"
    settings = Settings(
        data_root=data_root,
        desktop_session_token=session_token,
        scheduler_poll_seconds=0,
    )

    with TestClient(create_app(settings)) as client:
        stable_token = settings.owner_token_path.read_text(encoding="utf-8").strip()
        agent_token = settings.agent_token_path.read_text(encoding="utf-8").strip()
        _assert_owner_access(client, stable_token, 200)
        _assert_owner_access(client, session_token, 200)
        _assert_owner_access(client, agent_token, 401)
        _assert_owner_access(client, "cp_owner_random-invalid", 401)


def test_desktop_session_rotates_while_persistent_owner_survives(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "runtime"
    session_a = "cp_desktop_session-a"
    first = Settings(
        data_root=data_root,
        desktop_session_token=session_a,
        scheduler_poll_seconds=0,
    )
    with TestClient(create_app(first)) as client:
        stable_token = first.owner_token_path.read_text(encoding="utf-8").strip()
        _assert_owner_access(client, stable_token, 200)
        _assert_owner_access(client, session_a, 200)

    session_b = "cp_desktop_session-b"
    second = Settings(
        data_root=data_root,
        desktop_session_token=session_b,
        scheduler_poll_seconds=0,
    )
    with TestClient(create_app(second)) as client:
        assert (
            second.owner_token_path.read_text(encoding="utf-8").strip() == stable_token
        )
        _assert_owner_access(client, stable_token, 200)
        _assert_owner_access(client, session_b, 200)
        _assert_owner_access(client, session_a, 401)


def test_standalone_owner_override_does_not_accept_stale_file_token(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "runtime"
    generated = Settings(data_root=data_root)
    stale_file_token, _agent_token = generated.prepare()
    environment_owner = "cp_owner_environment-override"
    settings = Settings(
        data_root=data_root,
        owner_token=environment_owner,
        scheduler_poll_seconds=0,
    )

    with TestClient(create_app(settings)) as client:
        _assert_owner_access(client, environment_owner, 200)
        _assert_owner_access(client, stale_file_token, 401)


def test_secretary_desktop_origin_is_cors_allowed(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path / "runtime", scheduler_poll_seconds=0)
    with TestClient(create_app(settings)) as client:
        response = client.options(
            "/api/v1/workspaces/ws_default/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:17818",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,x-owner-token",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ("http://127.0.0.1:17818")
