"""Compile a source tree inside a target chroot and wrap it into a .deb.

Reproducibility measures applied here:
  * fixed build path (/build/src) and -ffile-prefix-map, so no host paths leak;
  * SOURCE_DATE_EPOCH exported to the build and used as every file's mtime;
  * LC_ALL=C.UTF-8, TZ=UTC, umask 022, root:root ownership in the archive;
  * the toolchain itself is pinned by the chroot (see chroot.py);
  * dpkg-deb from inside the chroot, so the archiver version is pinned too.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path

from xarch_pkg.chroot import Chroot
from xarch_pkg.config import PackageConfig
from xarch_pkg.util import BuildError, log

BUILD_DIR = "build"  # inside the chroot
SRC = f"/{BUILD_DIR}/src"
PKGROOT = f"/{BUILD_DIR}/pkgroot"
OUT = f"/{BUILD_DIR}/out"
_IGNORE = shutil.ignore_patterns(".git", ".xpkg-work", "__pycache__", "*.o", "*.so*", "*.deb")


def _copy_source(cfg: PackageConfig, ch: Chroot) -> None:
    build = ch.root / BUILD_DIR
    if build.exists():
        shutil.rmtree(build)
    shutil.copytree(cfg.source_dir, ch.root / SRC.lstrip("/"), ignore=_IGNORE, symlinks=True)
    (ch.root / PKGROOT.lstrip("/")).mkdir(parents=True)
    (ch.root / OUT.lstrip("/")).mkdir(parents=True)


def _compile(cfg: PackageConfig, ch: Chroot) -> None:
    env = {
        "SOURCE_DATE_EPOCH": str(cfg.source_date_epoch),
        "DESTDIR": PKGROOT,
        "DEB_HOST_ARCH": ch.target.name,
        "DEB_HOST_MULTIARCH": ch.target.triplet,
        "CFLAGS": f"-O2 -g0 -ffile-prefix-map={SRC}=.",
        "LDFLAGS": "-Wl,--build-id=sha1",
    }
    env.update(cfg.build_env)
    log(f"compiling {cfg.name} for {ch.target.name}")
    ch.sh(f"set -e; umask 022; cd {SRC}; {cfg.build_command}", env=env)


def _installed_size_kib(root: Path) -> int:
    """Same rule as dpkg-gencontrol: 1 KiB per dir/link, ceil(size/1024) per file."""
    total = 0
    for p in root.rglob("*"):
        if p.relative_to(root).parts[0] == "DEBIAN":
            continue
        if p.is_symlink() or p.is_dir():
            total += 1
        else:
            total += -(-p.stat().st_size // 1024)
    return total


def render_control(cfg: PackageConfig, arch: str, installed_size: int) -> str:
    fields = [
        ("Package", cfg.name),
        ("Version", cfg.version),
        ("Architecture", arch),
        ("Maintainer", cfg.maintainer),
        ("Installed-Size", str(installed_size)),
    ]
    fields += [(name, ", ".join(values)) for name, values in cfg.relations.items()]
    fields += [("Section", cfg.section), ("Priority", cfg.priority)]
    if cfg.homepage:
        fields.append(("Homepage", cfg.homepage))
    desc = cfg.description
    if cfg.long_description:
        body = []
        for line in cfg.long_description.splitlines():
            body.append(" " + line if line.strip() else " .")
        desc += "\n" + "\n".join(body)
    fields.append(("Description", desc))
    return "".join(f"{k}: {v}\n" for k, v in fields)


def _write_debian_dir(cfg: PackageConfig, pkgroot: Path, arch: str) -> None:
    if not any(pkgroot.iterdir()):
        raise BuildError("build produced no files; does the build command honour $DESTDIR?")
    debian = pkgroot / "DEBIAN"
    debian.mkdir(mode=0o755)

    md5_lines = []
    for p in sorted(pkgroot.rglob("*")):
        rel = p.relative_to(pkgroot)
        if rel.parts[0] == "DEBIAN" or p.is_symlink() or not p.is_file():
            continue
        md5_lines.append(f"{hashlib.md5(p.read_bytes()).hexdigest()}  {rel.as_posix()}\n")
    (debian / "md5sums").write_text("".join(md5_lines))

    for name, script in cfg.maintainer_scripts.items():
        dest = debian / name
        shutil.copyfile(script, dest)
        dest.chmod(0o755)

    (debian / "control").write_text(render_control(cfg, arch, _installed_size_kib(pkgroot)))


def _normalize(root: Path, epoch: int) -> None:
    """Deterministic permissions and timestamps for everything in the package."""
    for p in [root, *root.rglob("*")]:
        if not p.is_symlink():
            mode = p.stat().st_mode
            executable = mode & stat.S_IXUSR or p.is_dir()
            p.chmod(0o755 if executable else 0o644)
        os.utime(p, (epoch, epoch), follow_symlinks=False)


def build_deb(cfg: PackageConfig, ch: Chroot, out_dir: Path) -> Path:
    """Run the whole compile -> stage -> dpkg-deb pipeline; return the .deb path."""
    arch = ch.target.name
    _copy_source(cfg, ch)
    _compile(cfg, ch)

    pkgroot = ch.root / PKGROOT.lstrip("/")
    _write_debian_dir(cfg, pkgroot, arch)
    _normalize(pkgroot, cfg.source_date_epoch)

    filename = cfg.deb_filename(arch)
    ch.sh(
        f"dpkg-deb --root-owner-group -Zxz --build {PKGROOT} {OUT}/{filename}",
        env={"SOURCE_DATE_EPOCH": str(cfg.source_date_epoch)},
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / filename
    shutil.copyfile(ch.root / OUT.lstrip("/") / filename, dest)
    log(f"built {dest} ({dest.stat().st_size} bytes, sha256 {sha256(dest)[:16]}...)")
    return dest


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
