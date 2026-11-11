"""GPG signing of built packages.

For each .deb this produces
  * ``<deb>.asc``      -- an armored detached signature over the .deb itself;
  * ``<name>_<ver>_<arch>.changes`` -- a Debian upload manifest (Format 1.8)
    listing the .deb with its size and md5/sha1/sha256, clear-signed with the
    same key, which is what dput/dak and reprepro expect.

``generate_demo_key`` creates a throwaway, passphrase-less ed25519 key in an
isolated GNUPGHOME so the pipeline can be demonstrated end to end; for real
releases point --gnupg-home at a keyring holding your own key.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from xarch_pkg.config import PackageConfig
from xarch_pkg.util import BuildError, log, run

DEMO_UID = "xarch-pkg demo signing key <xarch-pkg-demo@example.invalid>"


class Signer:
    def __init__(self, gnupg_home: Path, key_id: str | None = None) -> None:
        self.home = gnupg_home.resolve()
        self.key_id = key_id

    def _gpg(self, *args: str, capture: bool = False, input_text: str | None = None):
        env = {**os.environ, "GNUPGHOME": str(self.home)}
        return run(
            ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", *args],
            env=env, capture=capture, input_text=input_text,
        )

    def generate_demo_key(self) -> str:
        self.home.mkdir(parents=True, exist_ok=True)
        self.home.chmod(0o700)
        existing = self._fingerprints()
        if existing:
            self.key_id = existing[0]
            log(f"reusing signing key {self.key_id}")
            return self.key_id
        params = (
            "%no-protection\n"
            "Key-Type: eddsa\nKey-Curve: ed25519\nKey-Usage: sign\n"
            "Name-Real: xarch-pkg demo signing key\n"
            "Name-Email: xarch-pkg-demo@example.invalid\n"
            "Expire-Date: 1y\n%commit\n"
        )
        self._gpg("--gen-key", input_text=params)
        self.key_id = self._fingerprints()[0]
        log(f"generated throwaway signing key {self.key_id}")
        return self.key_id

    def _fingerprints(self) -> list[str]:
        out = self._gpg("--list-secret-keys", "--with-colons", capture=True).stdout
        return [ln.split(":")[9] for ln in out.splitlines() if ln.startswith("fpr:")][:1]

    def _key_args(self) -> list[str]:
        if not self.key_id:
            fprs = self._fingerprints()
            if not fprs:
                raise BuildError(f"no secret key in {self.home}; run `xarch-pkg gen-key` first")
            self.key_id = fprs[0]
        return ["--local-user", self.key_id]

    def export_public(self, dest: Path) -> Path:
        self._gpg("--armor", "--output", str(dest), "--export", *self._key_args()[1:])
        return dest

    def detach_sign(self, path: Path) -> Path:
        sig = path.with_name(path.name + ".asc")
        self._gpg(*self._key_args(), "--armor", "--detach-sign", "--output", str(sig), str(path))
        return sig

    def clearsign(self, path: Path) -> None:
        tmp = path.with_name(path.name + ".tmp")
        self._gpg(*self._key_args(), "--clearsign", "--output", str(tmp), str(path))
        tmp.replace(path)

    def verify(self, *args: str) -> None:
        self._gpg("--verify", *args, capture=True)


def write_changes(cfg: PackageConfig, deb: Path, arch: str) -> Path:
    data = deb.read_bytes()
    size = len(data)
    md5, sha1, sha256 = (hashlib.new(h, data).hexdigest() for h in ("md5", "sha1", "sha256"))
    date = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(cfg.source_date_epoch))
    version = cfg.version.split(":", 1)[-1]
    changes = (
        "Format: 1.8\n"
        f"Date: {date}\n"
        f"Source: {cfg.name}\n"
        f"Binary: {cfg.name}\n"
        f"Architecture: {arch}\n"
        f"Version: {cfg.version}\n"
        f"Distribution: {cfg.suite}\n"
        "Urgency: medium\n"
        f"Maintainer: {cfg.maintainer}\n"
        f"Changed-By: {cfg.maintainer}\n"
        "Description:\n"
        f" {cfg.name} - {cfg.description}\n"
        "Changes:\n"
        f" {cfg.name} ({cfg.version}) {cfg.suite}; urgency=medium\n"
        " .\n"
        f"   * Built for {arch} by xarch-pkg.\n"
        f"Checksums-Sha1:\n {sha1} {size} {deb.name}\n"
        f"Checksums-Sha256:\n {sha256} {size} {deb.name}\n"
        f"Files:\n {md5} {size} {cfg.section} {cfg.priority} {deb.name}\n"
    )
    path = deb.with_name(f"{cfg.name}_{version}_{arch}.changes")
    path.write_text(changes)
    return path


def sign_package(signer: Signer, cfg: PackageConfig, deb: Path, arch: str) -> list[Path]:
    sig = signer.detach_sign(deb)
    changes = write_changes(cfg, deb, arch)
    signer.clearsign(changes)
    signer.verify(str(sig), str(deb))
    signer.verify(str(changes))
    log(f"signed {deb.name}: {sig.name}, {changes.name} (key {signer.key_id})")
    return [sig, changes]
