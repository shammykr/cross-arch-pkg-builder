"""QEMU user-mode emulation via binfmt_misc.

Registering an interpreter with the ``F`` (fix-binary) flag makes the kernel
open the static QEMU binary once, at registration time. Any foreign ELF that is
exec'd afterwards -- including inside a chroot that has no QEMU of its own --
is transparently run through that interpreter. That is what lets gcc, apt and
dpkg for arm64/armhf/riscv64 run unmodified inside the target chroots.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from xarch_pkg import arch as archmod
from xarch_pkg.util import BuildError, log, run

BINFMT_DIR = Path("/proc/sys/fs/binfmt_misc")


@dataclass
class Registration:
    name: str
    enabled: bool
    interpreter: str
    flags: str


def find_static_qemu(suffix: str) -> str:
    """Locate a statically linked qemu-<suffix> binary on the host."""
    for candidate in (f"qemu-{suffix}-static", f"qemu-{suffix}"):
        path = shutil.which(candidate)
        if path:
            return str(Path(path).resolve())
    raise BuildError(
        f"qemu-{suffix}-static not found; install it with scripts/setup-host.sh "
        "(apt-get install qemu-user-static)"
    )


def read_registration(suffix: str) -> Registration | None:
    entry = BINFMT_DIR / f"qemu-{suffix}"
    if not entry.exists():
        return None
    lines = entry.read_text().splitlines()
    info = dict(line.split(" ", 1) for line in lines[1:] if " " in line)
    return Registration(
        name=entry.name,
        enabled=lines[0].strip() == "enabled",
        interpreter=info.get("interpreter", "").strip(),
        flags=info.get("flags:", "").strip(),
    )


def ensure_binfmt_mounted() -> None:
    if (BINFMT_DIR / "register").exists():
        return
    log("mounting binfmt_misc")
    run(["mount", "-t", "binfmt_misc", "binfmt_misc", str(BINFMT_DIR)])


def register(target: str) -> Registration | None:
    """Make sure binaries for ``target`` can execute on this host.

    Returns the active registration, or ``None`` when no emulation is needed.
    An existing registration (e.g. from the qemu-user-static package) is reused
    as long as it is enabled; otherwise we write our own entry.
    """
    if not archmod.needs_emulation(target):
        return None
    a = archmod.get(target)
    ensure_binfmt_mounted()

    current = read_registration(a.qemu)
    if current and current.enabled:
        log(f"binfmt_misc: reusing {current.name} -> {current.interpreter} (flags {current.flags})")
        return current
    if current and not current.enabled:
        (BINFMT_DIR / current.name).write_text("-1")  # remove the disabled entry

    interpreter = find_static_qemu(a.qemu)
    rule = f":qemu-{a.qemu}:M::{a.binfmt_magic}:{a.binfmt_mask}:{interpreter}:F"
    log(f"binfmt_misc: registering qemu-{a.qemu} -> {interpreter}")
    try:
        (BINFMT_DIR / "register").write_text(rule)
    except OSError as exc:
        raise BuildError(f"could not register binfmt_misc handler: {exc}") from exc

    reg = read_registration(a.qemu)
    if not reg or not reg.enabled:
        raise BuildError(f"binfmt_misc registration for qemu-{a.qemu} did not take effect")
    return reg


def install_into_chroot(reg: Registration | None, root: Path) -> None:
    """Copy the interpreter into the chroot at the same path.

    Not needed when the handler has the F flag, but harmless, and it keeps the
    chroot usable on hosts whose existing registration lacks F.
    """
    if reg is None or "F" in reg.flags or not reg.interpreter:
        return
    dest = root / reg.interpreter.lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(reg.interpreter, dest)
