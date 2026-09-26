"""NTP sync gate (design §3.3): the kernel's sync flag via timedatectl.

``timedatectl show -p NTPSynchronized --value`` prints ``yes``/``no`` under
both systemd-timesyncd and chrony. A missing command or any failure yields
``None`` ("unknown"), which callers treat as unsynced.
"""

from __future__ import annotations

import shutil
import subprocess


def ntp_synchronized(runner=subprocess.run) -> bool | None:
    if shutil.which("timedatectl") is None:
        return None
    try:
        p = runner(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                   capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    out = p.stdout.strip().lower()
    if out == "yes":
        return True
    if out == "no":
        return False
    return None
