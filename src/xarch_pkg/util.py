"""Small helpers shared by the build steps."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


class BuildError(RuntimeError):
    """Raised when a build step fails in a way the user should see."""


def log(msg: str) -> None:
    print(f"[xarch-pkg] {msg}", file=sys.stderr, flush=True)


def run(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    capture: bool = False,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    log("$ " + shlex.join(cmd))
    proc = subprocess.run(
        cmd,
        env=env,
        text=True,
        input=input_text,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and proc.returncode != 0:
        detail = f"\n{proc.stderr}" if capture and proc.stderr else ""
        raise BuildError(f"command failed ({proc.returncode}): {shlex.join(cmd)}{detail}")
    return proc


def require_root(what: str) -> None:
    if os.geteuid() != 0:
        raise BuildError(f"{what} needs root (chroot, mount, binfmt_misc). Re-run with sudo.")


def give_to_invoking_user(path: Path) -> None:
    """When run via sudo, hand output files back to the user who called sudo."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if os.geteuid() != 0 or not uid or not gid:
        return
    for p in [path, *path.rglob("*")] if path.is_dir() else [path]:
        os.chown(p, int(uid), int(gid), follow_symlinks=False)
