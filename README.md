# cross-arch-pkg-builder

`xarch-pkg` builds Debian packages (`.deb`) for **amd64, arm64, armhf and riscv64**
from a single source tree on a single amd64 host. Each target gets its own
minimal Debian chroot, and foreign binaries (gcc, apt, dpkg…) run inside it through
**QEMU user-mode emulation** registered in `binfmt_misc`. Because the C code is
compiled *natively* inside the target's own userland, there's no cross-toolchain to
set up or keep in sync.

It is aimed at embedded/IoT developers and distribution maintainers who need
multi-arch `.deb`s without a build farm.

What a build does, per architecture:

1. registers the QEMU handler for the target in `binfmt_misc` (if it isn't native)
2. creates or reuses a Debian chroot (debootstrap, or a pre-built rootfs tarball)
3. installs a **pinned toolchain** (exact gcc / binutils / libc versions) and verifies it
4. compiles the source inside the chroot with `SOURCE_DATE_EPOCH` and fixed paths
5. generates `DEBIAN/control`, `md5sums` and maintainer scripts from `xpkg.toml`, then runs `dpkg-deb`
6. **validates** the result: control fields, the ELF architecture of every binary, and
   an `apt-get install --simulate` dependency resolution against the target archive
7. optionally **signs** it: a detached GPG signature plus a clear-signed `.changes`

## Architecture

```
                         xpkg.toml + source tree
                                   │
                          ┌────────▼────────┐
                          │  xarch-pkg CLI  │  cli.py / config.py
                          └────────┬────────┘
            ┌──────────────────────┼──────────────────────┐   one pass per --arch
            ▼                      ▼                      ▼
   ┌─────────────────┐   ┌───────────────────┐   ┌─────────────────┐
   │ qemu.py         │   │ chroot.py         │   │ builder.py      │
   │ binfmt_misc     │──▶│ debootstrap/tar   │──▶│ make in chroot  │
   │ qemu-*-static   │   │ pinned toolchain  │   │ control+scripts │
   │ (F flag)        │   │ /proc,/dev mounts │   │ dpkg-deb        │
   └─────────────────┘   └───────────────────┘   └────────┬────────┘
                                                          ▼
                          ┌───────────────────┐   ┌─────────────────┐
                          │ sign.py           │◀──│ validate.py     │
                          │ .deb.asc          │   │ dpkg-deb --info │
                          │ signed .changes   │   │ ELF e_machine   │
                          └─────────┬─────────┘   │ apt --simulate  │
                                    ▼             └─────────────────┘
                     dist/<name>_<ver>_<arch>.deb (+ .asc, .changes)
```

```
src/xarch_pkg/
  cli.py       Click commands: setup-qemu, chroot, build, validate, gen-key, sign
  config.py    xpkg.toml loading and validation (names, versions, relations)
  arch.py      arch table: QEMU name, GNU triplet, ELF machine, binfmt magic/mask
  qemu.py      binfmt_misc registration of static QEMU interpreters
  chroot.py    debootstrap / rootfs tarballs, toolchain pinning, running in the chroot
  builder.py   compile, stage, control file, md5sums, normalisation, dpkg-deb
  validate.py  post-build checks and apt resolver dry run
  sign.py      throwaway GPG key, detached signatures, .changes generation
examples/libhello/   example C shared library + its xpkg.toml
scripts/setup-host.sh  installs qemu-user-static, debootstrap, dpkg-dev, gnupg
```

## Requirements

* Ubuntu 22.04+ or Debian 12+ host (the CI runner is `ubuntu-24.04`)
* Python 3.11+
* root for the build itself, since chroot, mount and `binfmt_misc` need it

```bash
sudo ./scripts/setup-host.sh          # qemu-user-static, debootstrap, dpkg-dev, gnupg, ...
python3 -m venv .venv && .venv/bin/pip install .
```

## Usage

```bash
# Register QEMU handlers for the foreign targets (no-op for the host's own arch)
sudo .venv/bin/xarch-pkg setup-qemu

# Build one .deb per architecture (default: all four) into ./dist
sudo .venv/bin/xarch-pkg build examples/libhello

# Pick targets, check reproducibility, and sign with a throwaway key
sudo .venv/bin/xarch-pkg build examples/libhello -a arm64 -a riscv64 --repro-check --sign
```

| Command | Purpose |
|---|---|
| `setup-qemu [-a ARCH]…` | register `qemu-<arch>` in `binfmt_misc` (reuses existing handlers) |
| `chroot SRC [-a ARCH]… [--from-tarball T] [--export T]` | create/provision chroots, import/export rootfs tarballs |
| `build SRC [-a ARCH]… [-o DIR] [--rootfs-tarball T] [--repro-check] [--sign] [--key-id K]` | full pipeline |
| `validate SRC DEB…` | re-run the post-build checks on existing packages |
| `gen-key` | create a passphrase-less demo signing key in `<workdir>/gnupg` |
| `sign SRC DEB…` | sign existing packages and write `.changes` files |

`T` may be a path or an http(s) URL; `{arch}` in it is replaced with the target
(e.g. `--rootfs-tarball 'https://example.com/rootfs-{arch}.tar.gz'`).
Chroots are kept under `--workdir` (default `.xpkg-work/`) and reused by later builds.

## Package configuration (`xpkg.toml`)

```toml
[package]
name = "libhello1"
version = "1.0.0-1"
maintainer = "Shammy Kumar <shammykumarsbu27@gmail.com>"
description = "example greeting shared library"
section = "libs"
depends = ["libc6 (>= 2.34)"]       # also: pre_depends, recommends, suggests,
                                    # conflicts, breaks, provides, replaces

[maintainer_scripts]                # preinst / postinst / prerm / postrm
postinst = "debian/postinst"
postrm = "debian/postrm"

[build]
command = "make && make install PREFIX=/usr"   # runs in the chroot, must honour $DESTDIR
depends = []                                   # extra build dependencies to apt-get install
env = {}                                       # extra environment for the build command

[environment]
suite = "trixie"
mirror = "http://snapshot.debian.org/archive/debian/20261101T000000Z"
source_date_epoch = 1761955200      # defaults to the last git commit time

[toolchain]                         # exact versions, installed and verified in every chroot
gcc-14 = "14.2.0-19"
libc6-dev = "2.41-12+deb13u4"
```

Relation fields are syntax-checked when the config is loaded and checked again
against the real archive after the build.

## How the pieces work

**QEMU user-mode and binfmt_misc.** `setup-qemu` (and `build`, implicitly) writes a
`:qemu-aarch64:M::<magic>:<mask>:/usr/bin/qemu-aarch64-static:F` rule to
`/proc/sys/fs/binfmt_misc/register`. The `F` flag makes the kernel open the
interpreter at registration time, so any arm64 ELF exec'd afterwards, including
inside a chroot with no QEMU in it, runs through the emulator. If a handler already
exists (for example from the `qemu-user-static` package), it is reused. If that
handler lacks `F`, the interpreter binary is copied into the chroot as well.

**Chroots.** For a foreign arch, `debootstrap --foreign --variant=minbase` unpacks the
base system and `/debootstrap/debootstrap --second-stage` then runs *inside* the
chroot under QEMU. In CI the provisioned chroot is exported with
`chroot --export` and cached as a rootfs tarball, which `build --rootfs-tarball` seeds from.

**Reproducible builds.** Repeated builds of the same input give identical bytes because:
* the mirror is a `snapshot.debian.org` timestamp, so the whole archive is frozen;
* `[toolchain]` versions are installed with `pkg=version` and verified with
  `dpkg-query` (recorded in `/etc/xarch-pkg/toolchain.lock` in the chroot);
* `SOURCE_DATE_EPOCH` is exported to the build, every staged file's mtime is clamped to
  it, and `dpkg-deb` (also from the pinned chroot) uses it for archive timestamps;
* the source is always built at `/build/src` with `-ffile-prefix-map`, `LC_ALL=C.UTF-8`,
  `TZ=UTC` and `umask 022`, and the archive is written with `--root-owner-group`.

`--repro-check` builds every package twice and fails if the two `.deb`s differ.

**Validation.** After a build, the package is read back with `dpkg-deb --info` and
checked for required fields, the expected `Architecture`, and well-formed relation fields.
Every ELF file in the payload must have the target's `e_machine`, which catches an
accidental host-arch build. Finally,
`apt-get install --simulate ./pkg.deb` runs in the target chroot, which proves the
declared `Depends` can be satisfied from that architecture's archive.

**Signing.** `--sign` creates (or reuses) a throwaway ed25519 key in an isolated
`GNUPGHOME`. For each package it writes `pkg.deb.asc`, a detached armored signature,
and `pkg_<ver>_<arch>.changes`, a Format 1.8 upload manifest with size,
MD5/SHA1/SHA256 that is clear-signed with the same key. Both signatures are verified
right away, and the public key is exported to `dist/signing-key.asc`. Use
`--gnupg-home`/`--key-id` to sign with a real key.

## Walkthrough: the libhello example

`examples/libhello` is a tiny shared library (`hello.h`, `libhello.so.1`) whose
`hello_arch()` reports the CPU it was compiled for. Building it for arm64 on an amd64
machine:

```console
$ sudo .venv/bin/xarch-pkg build examples/libhello -a arm64 --repro-check --sign
[xarch-pkg] binfmt_misc: registering qemu-aarch64 -> /usr/bin/qemu-aarch64-static
[xarch-pkg] generated throwaway signing key 6581C756FE4E3CCAF13CFB8B7EF65C5FB66E65FE
[xarch-pkg] ===== libhello1 1.0.0-1 / arm64 =====
[xarch-pkg] debootstrap trixie/arm64 from http://snapshot.debian.org/archive/debian/20261101T000000Z
[xarch-pkg] $ chroot …/trixie-arm64 /debootstrap/debootstrap --second-stage
[xarch-pkg] toolchain verified: binutils=2.44-3, gcc=4:14.2.0-1, gcc-14=14.2.0-19, libc6-dev=2.41-12+deb13u4, make=4.4.1-2
[xarch-pkg] compiling libhello1 for arm64
[xarch-pkg] built dist/libhello1_1.0.0-1_arm64.deb (2980 bytes, sha256 29330b4d7735c326...)
[xarch-pkg] reproducible: second build is bit-identical (sha256 29330b4d…)
[xarch-pkg] validate libhello1_1.0.0-1_arm64.deb: OK; apt would install: libhello1
[xarch-pkg] signed libhello1_1.0.0-1_arm64.deb: libhello1_1.0.0-1_arm64.deb.asc, libhello1_1.0.0-1_arm64.changes

Summary:
  arm64    ok  libhello1_1.0.0-1_arm64.deb  sha256=29330b4d7735c326
```

The first run debootstraps the chroot, which takes a few minutes under emulation.
Later runs reuse it and take seconds. The result:

```console
$ dpkg-deb --info dist/libhello1_1.0.0-1_arm64.deb control
Package: libhello1
Version: 1.0.0-1
Architecture: arm64
Maintainer: Shammy Kumar <shammykumarsbu27@gmail.com>
Installed-Size: 73
Depends: libc6 (>= 2.34)
Section: libs
Priority: optional
Homepage: https://github.com/shammykr/cross-arch-pkg-builder
Description: example greeting shared library
 libhello is a tiny C shared library used to demonstrate building the
 ...

$ dpkg-deb --contents dist/libhello1_1.0.0-1_arm64.deb
drwxr-xr-x root/root         0 2025-11-01 00:00 ./
drwxr-xr-x root/root         0 2025-11-01 00:00 ./usr/
drwxr-xr-x root/root         0 2025-11-01 00:00 ./usr/include/
-rw-r--r-- root/root       548 2025-11-01 00:00 ./usr/include/hello.h
drwxr-xr-x root/root         0 2025-11-01 00:00 ./usr/lib/
drwxr-xr-x root/root         0 2025-11-01 00:00 ./usr/lib/aarch64-linux-gnu/
-rw-r--r-- root/root     67424 2025-11-01 00:00 ./usr/lib/aarch64-linux-gnu/libhello.so.1.0.0
lrwxrwxrwx root/root         0 2025-11-01 00:00 ./usr/lib/aarch64-linux-gnu/libhello.so -> libhello.so.1
lrwxrwxrwx root/root         0 2025-11-01 00:00 ./usr/lib/aarch64-linux-gnu/libhello.so.1 -> libhello.so.1.0.0

$ gpg --import dist/signing-key.asc
$ gpg --verify dist/libhello1_1.0.0-1_arm64.deb.asc dist/libhello1_1.0.0-1_arm64.deb
gpg: Good signature from "xarch-pkg demo signing key <xarch-pkg-demo@example.invalid>"
```

Every file's timestamp is `SOURCE_DATE_EPOCH`, and the arm64 `.deb` hashes to
`29330b4d…` every time, including on separate machines with freshly debootstrapped chroots.

## CI

`.github/workflows/build.yml` runs a matrix over all four architectures on
`ubuntu-24.04`. Each job installs QEMU, registers the handler, restores or creates the
cached rootfs tarball, and then runs `build --repro-check --sign`. It inspects and
verifies the package and uploads `dist/` as an artifact. amd64 and arm64 must pass.
armhf and riscv64 are marked `continue-on-error` because they depend on QEMU support
on the hosted runner.

## Known limitations

* Building needs root, and `binfmt_misc` is global to the kernel, so inside a container
  you need `--privileged`.
* Emulating amd64 on an arm64 host (`qemu-x86_64`) can crash in `ldconfig` during
  debootstrap's second stage. This is a QEMU limitation. On the intended amd64 host,
  amd64 builds run natively.

## License

MIT
