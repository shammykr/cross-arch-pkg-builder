"""Command-line interface: ``xarch-pkg``."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import click

from xarch_pkg import arch as archmod
from xarch_pkg import builder, chroot, config, qemu, sign, validate
from xarch_pkg.util import BuildError, give_to_invoking_user, log, require_root

ARCH_CHOICE = click.Choice(sorted(archmod.ARCHES))
DEFAULT_WORKDIR = Path(".xpkg-work")

workdir_option = click.option(
    "--workdir", type=click.Path(path_type=Path), default=DEFAULT_WORKDIR, show_default=True,
    help="Where chroots and the demo keyring live.",
)
gnupg_option = click.option(
    "--gnupg-home", type=click.Path(path_type=Path), default=None,
    help="GnuPG home holding the signing key [default: <workdir>/gnupg].",
)


def _arches(selected: tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(selected)) or list(archmod.ARCHES)


def _tarball_for(template: str | None, arch: str) -> str | None:
    return template.replace("{arch}", arch) if template else None


@click.group()
@click.version_option(package_name="cross-arch-pkg-builder")
def cli() -> None:
    """Build Debian packages for amd64, arm64, armhf and riscv64 from one source tree."""


@cli.command("setup-qemu")
@click.option("-a", "--arch", "arches", multiple=True, type=ARCH_CHOICE)
def setup_qemu(arches: tuple[str, ...]) -> None:
    """Register QEMU user-mode handlers in binfmt_misc for foreign ARCHES."""
    require_root("setup-qemu")
    for a in _arches(arches):
        reg = qemu.register(a)
        if reg is None:
            click.echo(f"{a:8} native on this host, no emulation needed")
        else:
            click.echo(f"{a:8} {reg.name} -> {reg.interpreter} (flags {reg.flags or '-'})")


@cli.command("chroot")
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("-a", "--arch", "arches", multiple=True, type=ARCH_CHOICE)
@click.option("--from-tarball", help="Seed from a rootfs tarball path/URL ({arch} is substituted).")
@click.option("--export", "export_tpl", help="Write the provisioned rootfs to this tarball ({arch} is substituted).")
@workdir_option
def chroot_cmd(source_dir: Path, arches, from_tarball, export_tpl, workdir: Path) -> None:
    """Create (or reuse) and provision the build chroot for each architecture."""
    require_root("chroot")
    cfg = config.load(source_dir)
    for a in _arches(arches):
        ch, _ = chroot.prepare(workdir, cfg, a, _tarball_for(from_tarball, a))
        with ch.mounted():
            ch.provision(cfg)
        if export_tpl:
            ch.export_tarball(Path(_tarball_for(export_tpl, a)))
        click.echo(f"{a:8} ready at {ch.root}")


@cli.command()
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("-a", "--arch", "arches", multiple=True, type=ARCH_CHOICE,
              help="Target architecture (repeatable). Default: all four.")
@click.option("-o", "--out", "out_dir", type=click.Path(path_type=Path), default=Path("dist"),
              show_default=True)
@click.option("--rootfs-tarball", help="Seed missing chroots from this tarball path/URL ({arch} is substituted).")
@click.option("--sign/--no-sign", "do_sign", default=False, help="GPG-sign each .deb and emit a signed .changes.")
@click.option("--key-id", help="Signing key; a throwaway demo key is generated if none exists.")
@click.option("--repro-check", is_flag=True, help="Build twice and require bit-identical .debs.")
@click.option("--no-validate", is_flag=True, help="Skip the post-build dependency validation.")
@workdir_option
@gnupg_option
def build(source_dir, arches, out_dir, rootfs_tarball, do_sign, key_id, repro_check, no_validate,
          workdir, gnupg_home) -> None:
    """Build SOURCE_DIR (which must contain xpkg.toml) into one .deb per architecture."""
    require_root("build")
    cfg = config.load(source_dir)
    signer = None
    if do_sign:
        signer = _signer(workdir, gnupg_home, key_id)
        if not key_id:
            signer.generate_demo_key()

    results: dict[str, str] = {}
    for a in _arches(arches):
        log(f"===== {cfg.name} {cfg.version} / {a} =====")
        try:
            ch, _ = chroot.prepare(workdir, cfg, a, _tarball_for(rootfs_tarball, a))
            with ch.mounted():
                ch.provision(cfg)
                deb = builder.build_deb(cfg, ch, out_dir)
                digest = builder.sha256(deb)
                if repro_check:
                    again = builder.build_deb(cfg, ch, out_dir / ".repro")
                    if builder.sha256(again) != digest:
                        raise BuildError(f"{deb.name} is not reproducible: two builds differ")
                    shutil.rmtree(out_dir / ".repro")
                    log(f"reproducible: second build is bit-identical (sha256 {digest})")
                if not no_validate:
                    validate.require_valid(deb, ch)
            if signer:
                sign.sign_package(signer, cfg, deb, a)
            results[a] = f"ok  {deb.name}  sha256={digest[:16]}"
        except BuildError as exc:
            log(f"{a}: {exc}")
            results[a] = f"FAILED  {str(exc).splitlines()[0]}"

    if signer:
        signer.export_public(out_dir / "signing-key.asc")
    give_to_invoking_user(out_dir)
    click.echo("\nSummary:")
    for a, line in results.items():
        click.echo(f"  {a:8} {line}")
    if any(r.startswith("FAILED") for r in results.values()):
        sys.exit(1)


@cli.command("validate")
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("debs", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path))
@workdir_option
def validate_cmd(source_dir: Path, debs: tuple[Path, ...], workdir: Path) -> None:
    """Check control fields, ELF arch and apt-resolvability of built DEBS."""
    require_root("validate")
    cfg = config.load(source_dir)
    failed = False
    for deb in debs:
        a = deb.name.rsplit("_", 1)[-1].removesuffix(".deb")
        ch, _ = chroot.prepare(workdir, cfg, a)
        with ch.mounted():
            failed |= not validate.validate(deb, ch).ok
    sys.exit(1 if failed else 0)


def _signer(workdir: Path, gnupg_home: Path | None, key_id: str | None) -> sign.Signer:
    return sign.Signer(gnupg_home or workdir / "gnupg", key_id)


@cli.command("gen-key")
@workdir_option
@gnupg_option
def gen_key(workdir: Path, gnupg_home: Path | None) -> None:
    """Generate a throwaway, passphrase-less signing key for demos and CI."""
    signer = _signer(workdir, gnupg_home, None)
    click.echo(signer.generate_demo_key())


@cli.command("sign")
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("debs", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--key-id")
@workdir_option
@gnupg_option
def sign_cmd(source_dir, debs, key_id, workdir, gnupg_home) -> None:
    """Detach-sign DEBS and write clear-signed .changes files next to them."""
    cfg = config.load(source_dir)
    signer = _signer(workdir, gnupg_home, key_id)
    for deb in debs:
        a = deb.name.rsplit("_", 1)[-1].removesuffix(".deb")
        sign.sign_package(signer, cfg, deb, a)


def main() -> None:
    try:
        cli(standalone_mode=True)
    except BuildError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
