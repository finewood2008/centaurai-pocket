from __future__ import annotations

import json
import os

import pytest

from centaur_pocket.desktop import notify_electron_ready


def test_desktop_ready_descriptor_uses_private_inherited_fd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv("CENTAURAI_POCKET_DESKTOP_READY_FD", str(write_fd))
    monkeypatch.setenv("CENTAURAI_POCKET_DESKTOP_NONCE", "nonce-for-test")

    notify_electron_ready(port=18718)

    with os.fdopen(read_fd, encoding="utf-8") as stream:
        descriptor = json.loads(stream.read())
    assert descriptor == {
        "nonce": "nonce-for-test",
        "pid": os.getpid(),
        "port": 18718,
    }


def test_desktop_ready_descriptor_requires_electron_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CENTAURAI_POCKET_DESKTOP_READY_FD", raising=False)
    monkeypatch.delenv("CENTAURAI_POCKET_DESKTOP_NONCE", raising=False)

    with pytest.raises(RuntimeError, match="就绪通道"):
        notify_electron_ready(port=8718)
