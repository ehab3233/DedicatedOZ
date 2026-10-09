#!/usr/bin/env bash
# Build the DedicatedOZ installer ramdisk.
#
# One image serves all three rails -- install, rescue and wipe. It boots, reads
# doz_* from /proc/cmdline, fetches its provisioning script from the control
# plane, and runs it. Everything mode-specific lives in that script, which
# means changing provisioning behaviour does not mean rebuilding the image.
#
# Built on Alpine: it is small enough to netboot quickly over 1G, and its
# packages cover everything the rails need (storcli is added separately --
# Broadcom does not redistribute it).
#
#     sudo ./build-ramdisk.sh --out ./assets/doz-installer
#
# Requires: docker, cpio, gzip.

set -euo pipefail

ALPINE_VERSION="${ALPINE_VERSION:-3.20}"
# Any Alpine mirror; packages are signature-checked either way.
ALPINE_MIRROR="${ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}"
OUT_DIR="./assets/doz-installer"
STORCLI_DEB="${STORCLI_DEB:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT_DIR="$2"; shift 2 ;;
        --storcli) STORCLI_DEB="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# Assemble the root where device files work. Ubuntu 25.10 and later mount
# /tmp as tmpfs with nodev: /dev/null in the new root then cannot be opened,
# and every package script line that writes to it is silently skipped.
mkdir -p "$OUT_DIR"
WORK=""
for base in "${DOZ_RAMDISK_WORKDIR:-}" /var/tmp "$(cd "$OUT_DIR/.." && pwd)" "${TMPDIR:-/tmp}"; do
    [ -n "$base" ] && [ -d "$base" ] || continue
    if command -v findmnt >/dev/null 2>&1 \
        && findmnt -no OPTIONS -T "$base" 2>/dev/null | tr ',' '\n' | grep -qx nodev; then
        continue
    fi
    WORK="$(mktemp -d "$base/doz-ramdisk.XXXXXX")" && break
done
[ -n "$WORK" ] || WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> building rootfs from alpine:$ALPINE_VERSION"

# Packages, and why each is here:
#   curl        -- fetch the provision script and post callbacks
#   openssh     -- rescue mode
#   hdparm      -- ATA secure erase
#   nvme-cli    -- nvme format
#   sg3_utils   -- sg_format for SAS drives
#   kexec-tools -- hand off to the distribution installer
#   util-linux / e2fsprogs / parted -- wipefs, blockdev, partitioning
#   pciutils    -- identifying the RAID controller when storcli misbehaves
docker run --rm -v "$WORK:/out" \
    -e ALPINE_VERSION="$ALPINE_VERSION" -e ALPINE_MIRROR="$ALPINE_MIRROR" \
    "alpine:$ALPINE_VERSION" sh -euc '
    # A fresh --root has no signing keys, so apk cannot verify the index,
    # calls it UNTRUSTED and then reports every package as missing. Give the
    # new root the keys and repositories the image itself trusts.
    mkdir -p /out/etc/apk/keys
    cp /etc/apk/keys/* /out/etc/apk/keys/
    printf "%s\n" "$ALPINE_MIRROR/v$ALPINE_VERSION/main" \
        "$ALPINE_MIRROR/v$ALPINE_VERSION/community" > /out/etc/apk/repositories
    apk add --no-cache --initdb --root /out \
        alpine-baselayout busybox openrc \
        linux-lts linux-firmware-none \
        curl ca-certificates \
        openssh openssh-server \
        hdparm nvme-cli sg3_utils \
        kexec-tools \
        util-linux e2fsprogs parted \
        pciutils eudev \
        bash

    # Drivers an installer on a rack server never uses. Without them the
    # initrd is a third the size: quicker to fetch over iPXE, less RAM.
    kver=$(ls /out/lib/modules)
    cd "/out/lib/modules/$kver/kernel"
    rm -rf sound drivers/gpu drivers/media drivers/net/wireless drivers/bluetooth \
        drivers/staging drivers/iio drivers/infiniband drivers/isdn drivers/video \
        drivers/input/joystick drivers/input/tablet drivers/input/touchscreen \
        net/wireless net/mac80211 net/bluetooth
    chroot /out depmod -a "$kver"
'

echo "==> installing the init script"
cat > "$WORK/init" <<'INIT'
#!/bin/sh
# PID 1 for the installer ramdisk.
#
# Its only job is to get networking up and hand control to the provisioning
# script the control plane renders for this specific job.

set -u

mount -t proc none /proc
mount -t sysfs none /sys
mount -t devtmpfs none /dev 2>/dev/null || true
mkdir -p /dev/pts && mount -t devpts none /dev/pts

echo "[doz] installer ramdisk booting"

/sbin/udevd --daemon 2>/dev/null || true
# --action=add: udevadm's default is "change", and the rule that loads
# drivers (80-drivers.rules) only runs on "add". Without it no NIC, RAID or
# disk driver loads and there is nothing to install over or onto.
/sbin/udevadm trigger --type=subsystems --action=add 2>/dev/null || true
/sbin/udevadm trigger --type=devices --action=add 2>/dev/null || true
/sbin/udevadm settle --timeout=30 2>/dev/null || true
# Belt and braces: load a driver for every device the kernel can name, the
# way Alpine's own initramfs does, in case udev missed any.
find /sys/devices -name modalias -exec cat {} + 2>/dev/null | sort -u \
    | xargs -r modprobe -abq 2>/dev/null || true
/sbin/udevadm settle --timeout=30 2>/dev/null || true
# NICs register a moment after their driver loads.
n=0
while [ "$n" -lt 10 ] && [ "$(ls /sys/class/net | grep -vc '^lo$')" -eq 0 ]; do
    sleep 1; n=$((n + 1))
done

# Kernel parameters, set by the iPXE script.
for param in $(cat /proc/cmdline); do
    case "$param" in
        doz_url=*)  DOZ_URL="${param#doz_url=}" ;;
        doz_mac=*)  DOZ_MAC="${param#doz_mac=}" ;;
        doz_sig=*)  DOZ_SIG="${param#doz_sig=}" ;;
        doz_job=*)  DOZ_JOB="${param#doz_job=}" ;;
        doz_mode=*) DOZ_MODE="${param#doz_mode=}" ;;
    esac
done

: "${DOZ_URL:=}" "${DOZ_MAC:=}" "${DOZ_SIG:=}"

fail() {
    echo "[doz] FATAL: $*"
    echo "[doz] dropping to a shell. The serial console is attached."
    # PID 1 must never exit (the kernel panics), so a shell that is closed
    # is simply started again.
    while true; do /bin/sh </dev/console >/dev/console 2>&1; done
}

[ -n "$DOZ_URL" ] || fail "no doz_url on the kernel command line"

echo "[doz] bringing up networking"
ip link set lo up
# DHCP on the port that PXE booted (doz_mac) first. A C220 has two LOM
# ports and often a VIC, and the first one the kernel lists is not
# necessarily the one with a cable. Then every other port, in case the MAC
# was recorded wrong.
want=$(echo "$DOZ_MAC" | tr 'A-F-' 'a-f:')
ifaces=""
for path in /sys/class/net/*; do
    name=${path##*/}
    [ "$name" = lo ] && continue
    if [ -n "$want" ] && [ "$(cat "$path/address" 2>/dev/null)" = "$want" ]; then
        ifaces="$name $ifaces"
    else
        ifaces="$ifaces $name"
    fi
done
[ -n "$ifaces" ] || fail "no network interfaces: the NIC driver did not load"
for iface in $ifaces; do ip link set "$iface" up; done
DOZ_IFACE=""
for iface in $ifaces; do
    echo "[doz] DHCP on $iface ($(cat "/sys/class/net/$iface/address"))"
    # udhcpc backgrounds itself once it has a lease and keeps renewing it.
    if udhcpc -i "$iface" -t 6 -T 3 -n; then DOZ_IFACE=$iface; break; fi
done
[ -n "$DOZ_IFACE" ] || fail "DHCP failed on every interface:$ifaces"
export DOZ_IFACE

echo "[doz] fetching provisioning script for $DOZ_MAC"
i=1
while [ "$i" -le 10 ]; do
    if curl -sf -m 30 -o /provision.sh \
        "$DOZ_URL/boot/provision/$DOZ_MAC?sig=$DOZ_SIG"; then
        break
    fi
    echo "[doz] attempt $i failed, retrying"
    sleep 5
    i=$((i + 1))
done
[ -s /provision.sh ] || fail "could not fetch the provisioning script from $DOZ_URL"

chmod +x /provision.sh
echo "[doz] handing off to the provisioning script (mode=${DOZ_MODE:-unknown})"
/provision.sh

# provision.sh is not supposed to return. If it does, something in the trap
# handling went wrong and a shell is more useful than a reboot loop.
fail "provisioning script exited unexpectedly"
INIT
chmod +x "$WORK/init"

if [ -n "$STORCLI_DEB" ]; then
    echo "==> unpacking storcli from $STORCLI_DEB"
    mkdir -p "$WORK/opt/MegaRAID/storcli"
    ar p "$STORCLI_DEB" data.tar.gz | tar xz -C "$WORK" ./opt/MegaRAID/storcli 2>/dev/null \
        || echo "warning: could not unpack storcli; RAID configuration will be skipped"
else
    echo "==> WARNING: no --storcli given."
    echo "    Broadcom does not permit redistribution, so it has to be supplied"
    echo "    separately. Without it the ramdisk cannot build RAID arrays and"
    echo "    installs will land on bare disks."
fi

mkdir -p "$OUT_DIR"
KERNEL="$(find "$WORK/boot" -name 'vmlinuz-*' | head -n1)"
[ -n "$KERNEL" ] || { echo "no kernel found in the rootfs" >&2; exit 1; }
cp "$KERNEL" "$OUT_DIR/vmlinuz"
# iPXE fetches the kernel separately, and Alpine's own initramfs is never
# used, so /boot stays out of the image.
rm -rf "$WORK/boot"

echo "==> packing initrd"
( cd "$WORK" && find . -print0 | cpio --null -o --format=newc --quiet ) | gzip -9 > "$OUT_DIR/initrd.img"

echo
echo "==> done"
ls -lh "$OUT_DIR"
echo
echo "Serve $OUT_DIR at \$DOZ_BOOT_ASSET_BASE_URL/doz-installer/ and the"
echo "iPXE scripts will find it."
