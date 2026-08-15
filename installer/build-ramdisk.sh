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
OUT_DIR="./assets/doz-installer"
STORCLI_DEB="${STORCLI_DEB:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT_DIR="$2"; shift 2 ;;
        --storcli) STORCLI_DEB="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

WORK="$(mktemp -d)"
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
docker run --rm -v "$WORK:/out" "alpine:$ALPINE_VERSION" sh -euc '
    apk add --no-cache --initdb --root /out \
        --repository https://dl-cdn.alpinelinux.org/alpine/v'"$ALPINE_VERSION"'/main \
        --repository https://dl-cdn.alpinelinux.org/alpine/v'"$ALPINE_VERSION"'/community \
        alpine-baselayout busybox openrc \
        linux-lts linux-firmware-none \
        curl ca-certificates \
        openssh openssh-server \
        hdparm nvme-cli sg3_utils \
        kexec-tools \
        util-linux e2fsprogs parted \
        pciutils eudev \
        bash
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
/sbin/udevadm trigger 2>/dev/null || true
/sbin/udevadm settle --timeout=30 2>/dev/null || true

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
    exec /bin/sh
}

[ -n "$DOZ_URL" ] || fail "no doz_url on the kernel command line"

echo "[doz] bringing up networking"
ip link set lo up
for iface in $(ls /sys/class/net | grep -v '^lo$'); do
    ip link set "$iface" up
done
# The provisioning VLAN runs DHCP; that is how iPXE got here in the first place.
udhcpc -i "$(ls /sys/class/net | grep -v '^lo$' | head -n1)" -t 10 -T 3 -n \
    || fail "DHCP failed on the provisioning VLAN"

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

echo "==> packing initrd"
mkdir -p "$OUT_DIR"
( cd "$WORK" && find . -print0 | cpio --null -o --format=newc ) | gzip -9 > "$OUT_DIR/initrd.img"

KERNEL="$(find "$WORK/boot" -name 'vmlinuz-*' | head -n1)"
[ -n "$KERNEL" ] || { echo "no kernel found in the rootfs" >&2; exit 1; }
cp "$KERNEL" "$OUT_DIR/vmlinuz"

echo
echo "==> done"
ls -lh "$OUT_DIR"
echo
echo "Serve $OUT_DIR at \$DOZ_BOOT_ASSET_BASE_URL/doz-installer/ and the"
echo "iPXE scripts will find it."
