"""Electron-managed API sidecar bootstrap."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import socket
import sys

import uvicorn

from .config import Settings
from .main import create_app


def terminate_with_parent_on_linux() -> None:
    """Ask the kernel to stop the sidecar if its Electron parent disappears."""

    if sys.platform != "linux":
        return
    parent_pid = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    pr_set_pdeathsig = 1
    if libc.prctl(pr_set_pdeathsig, signal.SIGTERM, 0, 0, 0) != 0:
        return
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)


def notify_electron_ready(*, port: int) -> None:
    """Report the bound socket over Electron's private inherited pipe."""

    raw_fd = os.environ.get("CENTAURAI_POCKET_DESKTOP_READY_FD", "")
    nonce = os.environ.get("CENTAURAI_POCKET_DESKTOP_NONCE", "")
    if not raw_fd or not nonce:
        raise RuntimeError("缺少 Electron sidecar 就绪通道")
    ready_fd = int(raw_fd)
    payload = json.dumps(
        {"nonce": nonce, "pid": os.getpid(), "port": port},
        separators=(",", ":"),
    )
    with os.fdopen(ready_fd, "w", encoding="utf-8", closefd=True) as stream:
        stream.write(f"{payload}\n")
        stream.flush()


def run() -> None:
    settings = Settings.from_env()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((settings.host, settings.port))
        listener.listen(2048)
        bound_port = int(listener.getsockname()[1])
        notify_electron_ready(port=bound_port)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(settings),
                host=settings.host,
                port=bound_port,
            )
        )
        server.run(sockets=[listener])
    finally:
        listener.close()


def main() -> None:
    terminate_with_parent_on_linux()
    run()
