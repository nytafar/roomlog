"""systemd notify protocol over ``$NOTIFY_SOCKET`` (sd_notify(3)), no dependency."""

from __future__ import annotations

import os
import socket


def notify(message: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(message.encode())
        return True
    except OSError:
        return False


def ready() -> bool:
    return notify("READY=1")


def watchdog() -> bool:
    return notify("WATCHDOG=1")


def stopping() -> bool:
    return notify("STOPPING=1")


def status(text: str) -> bool:
    return notify(f"STATUS={text}")
