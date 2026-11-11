#!/usr/bin/env bash
# Install the host tools xarch-pkg needs on Ubuntu 22.04+ / Debian 12+.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$@"
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
    qemu-user-static binfmt-support debootstrap debian-archive-keyring \
    dpkg-dev gnupg ca-certificates python3 python3-venv

# Make sure binfmt_misc is mounted so handlers can be registered.
if [ ! -e /proc/sys/fs/binfmt_misc/register ]; then
    mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
fi

echo "host ready: $(debootstrap --version), $(qemu-aarch64-static --version 2>/dev/null | head -1 || true)"
