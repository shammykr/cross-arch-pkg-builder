"""Target architecture table: Debian arch name -> QEMU / ELF / binfmt details."""

from __future__ import annotations

import platform
from dataclasses import dataclass

# binfmt_misc magic/mask strings are taken from QEMU's scripts/qemu-binfmt-conf.sh.
# They are written to /proc/sys/fs/binfmt_misc/register verbatim; the kernel
# decodes the \xNN escapes itself.
_ELF64_MASK = r"\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff"
_ELF32_MASK = _ELF64_MASK


@dataclass(frozen=True)
class Arch:
    name: str  # Debian architecture name
    qemu: str  # QEMU user-mode suffix (qemu-<suffix>)
    triplet: str  # GNU multiarch triplet
    elf_class: int  # 1 = 32-bit, 2 = 64-bit
    elf_machine: int  # e_machine value in the ELF header
    binfmt_magic: str
    binfmt_mask: str


ARCHES: dict[str, Arch] = {
    "amd64": Arch(
        "amd64", "x86_64", "x86_64-linux-gnu", 2, 62,
        r"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x3e\x00",
        _ELF64_MASK,
    ),
    "arm64": Arch(
        "arm64", "aarch64", "aarch64-linux-gnu", 2, 183,
        r"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\xb7\x00",
        _ELF64_MASK,
    ),
    "armhf": Arch(
        "armhf", "arm", "arm-linux-gnueabihf", 1, 40,
        r"\x7fELF\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x28\x00",
        _ELF32_MASK,
    ),
    "riscv64": Arch(
        "riscv64", "riscv64", "riscv64-linux-gnu", 2, 243,
        r"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\xf3\x00",
        _ELF64_MASK,
    ),
}

_MACHINE_TO_DEB = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "armhf",
    "riscv64": "riscv64",
}


def get(name: str) -> Arch:
    try:
        return ARCHES[name]
    except KeyError:
        raise ValueError(f"unsupported architecture {name!r}; choose from {', '.join(ARCHES)}")


def host_arch() -> str:
    machine = platform.machine().lower()
    return _MACHINE_TO_DEB.get(machine, machine)


def needs_emulation(target: str) -> bool:
    """armhf code also runs natively on most arm64 CPUs, but not all, so emulate it."""
    return target != host_arch()
