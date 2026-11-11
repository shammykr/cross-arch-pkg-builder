"""Per-architecture Debian chroot build environments.

A chroot is created once per (suite, arch) with debootstrap -- or seeded from a
rootfs tarball -- and then reused. The toolchain packages listed in
``[toolchain]`` are installed at their exact versions and verified, so every
build of a package runs against the same gcc/libc/binutils.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shlex
import shutil
import tempfile
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from xarch_pkg import arch as archmod
from xarch_pkg import qemu
from xarch_pkg.config import PackageConfig
from xarch_pkg.util import BuildError, log, run

BASE_MARKER = ".xarch-pkg-base"  # debootstrap (both stages) finished
READY_MARKER = ".xarch-pkg-ready"  # toolchain provisioned; holds a spec hash
LOCK_FILE = "etc/xarch-pkg/toolchain.lock"
DEBIAN_KEYRING = Path("/usr/share/keyrings/debian-archive-keyring.gpg")
BASE_PACKAGES = ["ca-certificates"]
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class Chroot:
    def __init__(self, workdir: Path, suite: str, target: str) -> None:
        self.target = archmod.get(target)
        self.suite = suite
        self.root = (workdir / "chroots" / f"{suite}-{target}").resolve()
        self._mounted: list[Path] = []

    # ---------------------------------------------------------------- creation
    def exists(self) -> bool:
        return (self.root / BASE_MARKER).exists()

    def _reset(self) -> None:
        """Remove a half-created chroot left behind by an interrupted run."""
        if self.root.exists():
            log(f"removing incomplete chroot {self.root}")
            run(["rm", "-rf", "--one-file-system", str(self.root)])
        self.root.mkdir(parents=True)

    def debootstrap(self, mirror: str, reg: qemu.Registration | None) -> None:
        if not shutil.which("debootstrap"):
            raise BuildError("debootstrap is not installed (see scripts/setup-host.sh)")
        self._reset()
        cmd = ["debootstrap", f"--arch={self.target.name}", "--variant=minbase"]
        if DEBIAN_KEYRING.exists():
            cmd.append(f"--keyring={DEBIAN_KEYRING}")
        foreign = reg is not None
        if foreign:
            cmd.append("--foreign")
        cmd += [self.suite, str(self.root), mirror]
        # Older debootstrap releases may not know a newer suite name yet; every
        # Debian suite since buster uses the same script as sid.
        if not Path(f"/usr/share/debootstrap/scripts/{self.suite}").exists():
            cmd.append("/usr/share/debootstrap/scripts/sid")
        log(f"debootstrap {self.suite}/{self.target.name} from {mirror}")
        run(cmd)
        if foreign:
            qemu.install_into_chroot(reg, self.root)
            run(["chroot", str(self.root), "/debootstrap/debootstrap", "--second-stage"])

        # snapshot.debian.org serves Release files whose Valid-Until has passed.
        if "snapshot.debian.org" in mirror:
            conf = self.root / "etc/apt/apt.conf.d/99xarch-snapshot"
            conf.write_text('Acquire::Check-Valid-Until "false";\n')
        (self.root / "etc/apt/apt.conf.d/99xarch-noninteractive").write_text(
            'APT::Install-Recommends "false";\nAPT::Install-Suggests "false";\n'
            'Dpkg::Use-Pty "0";\n'
        )
        (self.root / BASE_MARKER).write_text(f"{self.suite} {self.target.name} {mirror}\n")

    def import_tarball(self, source: str) -> None:
        """Seed the chroot from a rootfs tarball (local path or http(s) URL)."""
        self._reset()
        with contextlib.ExitStack() as stack:
            path = Path(source)
            if source.startswith(("http://", "https://")):
                tmp = stack.enter_context(tempfile.TemporaryDirectory())
                path = Path(tmp) / "rootfs.tar"
                log(f"downloading rootfs {source}")
                with urllib.request.urlopen(source) as resp, path.open("wb") as fh:
                    shutil.copyfileobj(resp, fh)
            if not path.is_file():
                raise BuildError(f"rootfs tarball {source} not found")
            log(f"extracting rootfs {path} -> {self.root}")
            run(["tar", "--numeric-owner", "-xpf", str(path), "-C", str(self.root)])
        if not self.exists():
            raise BuildError(f"{source} is not a rootfs exported by `xarch-pkg chroot --export`")

    def export_tarball(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        run([
            "tar", "--numeric-owner", "-czpf", str(dest.resolve()), "-C", str(self.root),
            "--exclude=./proc/*", "--exclude=./sys/*", "--exclude=./dev/*", "--exclude=./build",
            ".",
        ])
        log(f"exported rootfs to {dest}")

    # ------------------------------------------------------------ provisioning
    def provision(self, cfg: PackageConfig) -> None:
        """Install the pinned toolchain and build dependencies (idempotent)."""
        spec = {"toolchain": cfg.toolchain, "depends": sorted(cfg.build_depends)}
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        marker = self.root / READY_MARKER
        if marker.exists() and marker.read_text().strip() == digest:
            log(f"chroot {self.root.name} already provisioned")
            return

        pinned = [f"{pkg}={ver}" for pkg, ver in sorted(cfg.toolchain.items())]
        packages = BASE_PACKAGES + pinned + cfg.build_depends
        self.sh("apt-get update -q")
        # --allow-downgrades: a pin may be older than what debootstrap pulled in.
        self.sh(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -q --allow-downgrades "
            + shlex.join(packages)
        )
        self.verify_toolchain(cfg.toolchain)
        self.sh("apt-get clean")
        marker.write_text(digest + "\n")

    def verify_toolchain(self, toolchain: dict[str, str]) -> None:
        if not toolchain:
            return
        out = self.sh(
            "dpkg-query -W -f '${Package} ${Version}\\n' " + shlex.join(sorted(toolchain)),
            capture=True,
        ).stdout
        installed = dict(line.split(" ", 1) for line in out.splitlines() if line)
        wrong = {p: (v, installed.get(p)) for p, v in toolchain.items() if installed.get(p) != v}
        if wrong:
            detail = ", ".join(f"{p}: want {w}, got {g}" for p, (w, g) in wrong.items())
            raise BuildError(f"toolchain pin mismatch in {self.root.name}: {detail}")
        lock = self.root / LOCK_FILE
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("".join(f"{p} {installed[p]}\n" for p in sorted(toolchain)))
        log(f"toolchain verified: {', '.join(f'{p}={v}' for p, v in sorted(toolchain.items()))}")

    # ---------------------------------------------------------------- running
    @contextlib.contextmanager
    def mounted(self) -> Iterator[Chroot]:
        """Bind the bits of the host a build needs: /proc, /dev, DNS config."""
        try:
            for name, args in (
                ("proc", ["-t", "proc", "proc"]),
                ("dev", ["--bind", "/dev"]),
                ("dev/pts", ["--bind", "/dev/pts"]),
            ):
                target = self.root / name
                target.mkdir(parents=True, exist_ok=True)
                run(["mount", *args, str(target)])
                self._mounted.append(target)
            resolv = self.root / "etc/resolv.conf"
            if resolv.is_symlink():
                resolv.unlink()
            shutil.copy2("/etc/resolv.conf", resolv)
            yield self
        finally:
            for target in reversed(self._mounted):
                run(["umount", "-l", str(target)], check=False)
            self._mounted.clear()

    def sh(self, script: str, *, env: dict[str, str] | None = None, capture: bool = False):
        """Run a shell snippet inside the chroot with a clean environment."""
        base = {"PATH": DEFAULT_PATH, "HOME": "/root", "LC_ALL": "C.UTF-8", "TZ": "UTC"}
        base.update(env or {})
        assignments = [f"{k}={v}" for k, v in base.items()]
        return run(
            ["chroot", str(self.root), "/usr/bin/env", "-i", *assignments, "/bin/sh", "-c", script],
            capture=capture,
        )


def prepare(
    workdir: Path,
    cfg: PackageConfig,
    target: str,
    rootfs_tarball: str | None = None,
) -> tuple[Chroot, qemu.Registration | None]:
    """Return a ready-to-use chroot for ``target``, creating it if needed."""
    reg = qemu.register(target)
    ch = Chroot(workdir, cfg.suite, target)
    if not ch.exists():
        if rootfs_tarball:
            ch.import_tarball(rootfs_tarball)
        else:
            ch.debootstrap(cfg.mirror, reg)
    qemu.install_into_chroot(reg, ch.root)
    return ch, reg


__all__ = ["Chroot", "prepare"]
