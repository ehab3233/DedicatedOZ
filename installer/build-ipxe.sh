#!/usr/bin/env bash
# Build iPXE boot loaders with the management server's address built in.
#
# A NIC's PXE ROM fetches undionly.kpxe (BIOS) or ipxe.efi (UEFI) over TFTP.
# Stock iPXE then asks DHCP again and boots whatever filename it is given,
# which on most DHCP servers is that same loader again, forever. A user-class
# rule does not reliably help: MikroTik's boot-file-name, for one, fills the
# BOOTP "file" field, which iPXE reads in preference to option 67. These
# loaders carry a script that ignores the DHCP filename: get an address, then
# chain straight to the control plane. Any DHCP server then only needs
# next-server and a filename.
#
#     sudo ./build-ipxe.sh --url http://10.0.0.5 --out /opt/doz/installer/tftp
#
# Requires docker. Takes a few minutes: iPXE is compiled from source,
# downloaded from GitHub, or taken from IPXE_SRC (an iPXE checkout) when set.

set -euo pipefail

IPXE_REF="${IPXE_REF:-v2.0.0}"
ALPINE_VERSION="${ALPINE_VERSION:-3.20}"
URL=""
OUT_DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --url) URL="${2%/}"; shift 2 ;;
        --out) OUT_DIR="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ -n "$URL" ] && [ -n "$OUT_DIR" ] || { echo "usage: $0 --url http://MGMT_IP --out DIR" >&2; exit 2; }
case "$URL" in http://*|https://*) ;; *) echo "--url must start with http:// or https://" >&2; exit 2 ;; esac

mkdir -p "$OUT_DIR"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/doz-ipxe.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# ${netX/mac}: the interface dhcp just configured. With ipxe.efi every NIC
# is a net device, and net0 is not necessarily the one with the cable.
cat > "$WORK/embed.ipxe" <<EMBED
#!ipxe
echo DedicatedOZ network boot: ${URL}
:dhcp
dhcp || goto dhcp_retry
chain ${URL}/boot/ipxe?mac=\${netX/mac} || goto unreachable
:dhcp_retry
echo DHCP failed; retrying in 5 seconds
sleep 5
goto dhcp
:unreachable
echo Could not fetch ${URL}/boot/ipxe; booting the local disk in 10 seconds
sleep 10
sanboot --no-describe --drive 0x80 || exit
EMBED

SRC_MOUNT=()
if [ -n "${IPXE_SRC:-}" ]; then
    [ -f "$IPXE_SRC/src/Makefile" ] || { echo "IPXE_SRC=$IPXE_SRC is not an iPXE checkout" >&2; exit 2; }
    SRC_MOUNT=(-v "$(cd "$IPXE_SRC" && pwd):/ipxe-src:ro")
    echo "==> building iPXE from $IPXE_SRC for $URL"
else
    echo "==> building iPXE $IPXE_REF for $URL"
fi
docker run --rm -v "$WORK:/work" "${SRC_MOUNT[@]}" -e IPXE_REF="$IPXE_REF" \
    "alpine:$ALPINE_VERSION" sh -euc '
    apk add --no-cache build-base perl xz-dev curl >/dev/null
    if [ -d /ipxe-src ]; then
        mkdir -p /tmp/ipxe-src && cp -a /ipxe-src/. /tmp/ipxe-src/
    else
        curl -fsSL "https://github.com/ipxe/ipxe/archive/refs/tags/$IPXE_REF.tar.gz" | tar -xz -C /tmp
    fi
    cd /tmp/ipxe-*/src
    make -j"$(nproc)" NO_WERROR=1 EMBED=/work/embed.ipxe \
        bin/undionly.kpxe bin-x86_64-efi/ipxe.efi >/work/build.log 2>&1 \
        || { tail -40 /work/build.log; exit 1; }
    cp bin/undionly.kpxe bin-x86_64-efi/ipxe.efi /work/
'

for f in undionly.kpxe ipxe.efi; do
    install -m 644 "$WORK/$f" "$OUT_DIR/$f.new" && mv "$OUT_DIR/$f.new" "$OUT_DIR/$f"
done
# What these loaders chain to, so the installer can tell when they are stale.
echo "$URL" > "$OUT_DIR/.embedded-url"
cp "$WORK/embed.ipxe" "$OUT_DIR/.embedded.ipxe"

echo "==> done"
ls -l "$OUT_DIR/undionly.kpxe" "$OUT_DIR/ipxe.efi"
echo "These loaders chain to $URL/boot/ipxe whatever filename DHCP hands them."
