#!/usr/bin/env bash
# Fetch the netboot kernels, initrds and ISOs the default OS templates expect.
#
# Lays them out under installer/assets/os/ exactly where scripts/init_db.py
# points, so nothing has to be edited afterwards. Re-runnable: existing files
# are kept unless --force.
#
#   ./deploy/fetch-os-images.sh [--assets DIR] [--only ubuntu|debian|rocky] [--force]
#
# Needs: curl, and bsdtar (package libarchive-tools) to pull the kernel out
# of the Ubuntu ISO without mounting it.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ASSETS="$ROOT/installer/assets"
ONLY=""
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --assets) ASSETS="$2"; shift 2 ;;
        --only)   ONLY="$2"; shift 2 ;;
        --force)  FORCE=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# Pin point releases so a rebuilt asset directory is byte-identical to the
# last one. Bump deliberately, and re-run the bench test when you do.
UBUNTU_ISO_URL="https://releases.ubuntu.com/22.04/ubuntu-22.04.5-live-server-amd64.iso"
DEBIAN_NETBOOT="http://deb.debian.org/debian/dists/bookworm/main/installer-amd64/current/images/netboot/debian-installer/amd64"
ROCKY_PXEBOOT="https://dl.rockylinux.org/pub/rocky/9/BaseOS/x86_64/os/images/pxeboot"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

fetch() {
    # fetch <url> <dest>
    url="$1"; dest="$2"
    if [ -s "$dest" ] && [ "$FORCE" -eq 0 ]; then
        echo "  keeping $(basename "$dest")"
        return
    fi
    mkdir -p "$(dirname "$dest")"
    echo "  $url"
    curl -fL --progress-bar --retry 3 -o "$dest.part" "$url"
    mv "$dest.part" "$dest"
}

want() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }

if want ubuntu; then
    say "Ubuntu 22.04 (live server ISO + casper kernel/initrd)"
    dir="$ASSETS/os/ubuntu-22.04"
    iso="$dir/ubuntu-22.04-live-server-amd64.iso"
    fetch "$UBUNTU_ISO_URL" "$iso"
    if [ ! -s "$dir/casper/vmlinuz" ] || [ "$FORCE" -eq 1 ]; then
        command -v bsdtar >/dev/null || { echo "bsdtar missing: apt install libarchive-tools" >&2; exit 1; }
        mkdir -p "$dir/casper"
        echo "  extracting casper/vmlinuz and casper/initrd from the ISO"
        bsdtar -xf "$iso" -C "$dir" casper/vmlinuz casper/initrd
    else
        echo "  keeping casper/vmlinuz, casper/initrd"
    fi
    # The same ISO installs by hand through virtual media, so make it show in
    # the panel's ISO store too. A hard link costs nothing; a symlink if the
    # store is on another filesystem.
    store="$ASSETS/iso/$(basename "$iso")"
    if [ ! -e "$store" ]; then
        mkdir -p "$ASSETS/iso"
        ln "$iso" "$store" 2>/dev/null || ln -s "$iso" "$store"
        echo "  linked into the ISO store as iso/$(basename "$iso"); catalogue it with Scan directory on the Images page"
    fi
fi

if want debian; then
    say "Debian 12 netboot installer"
    fetch "$DEBIAN_NETBOOT/linux"     "$ASSETS/os/debian-12/linux"
    fetch "$DEBIAN_NETBOOT/initrd.gz" "$ASSETS/os/debian-12/initrd.gz"
fi

if want rocky; then
    say "Rocky Linux 9 pxeboot"
    fetch "$ROCKY_PXEBOOT/vmlinuz"    "$ASSETS/os/rocky-9/images/pxeboot/vmlinuz"
    fetch "$ROCKY_PXEBOOT/initrd.img" "$ASSETS/os/rocky-9/images/pxeboot/initrd.img"
fi

echo
say "done"
du -sh "$ASSETS/os"/* 2>/dev/null || true
echo
echo "These are served from \$DOZ_BOOT_ASSET_BASE_URL and listed on the panel's"
echo "Images page under Netboot images. The installer ramdisk itself is built"
echo "separately: sudo ./doz.sh ramdisk"
