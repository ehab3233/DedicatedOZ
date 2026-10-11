#!/usr/bin/env bash
# DedicatedOZ one-line installer for a fresh Ubuntu machine.
#
#   curl -fsSL https://raw.githubusercontent.com/ehab3233/DedicatedOZ/HEAD/install.sh | sudo bash
#
# Gets the code, installs the whole management server as the `doz` service,
# starts a simulated BMC so there is something to click on, and prints the
# panel address and admin login. Run it again later to update.
#
# Defaults are for a management VM that reaches the servers' CIMCs over
# routing rather than a flat VLAN: no DHCP/PXE is set up, so nothing on the
# network is touched. Power, consoles, sensors, the event log and ISO installs
# through virtual media all work that way. Override with environment variables:
#
#   DOZ_IP=10.0.0.5            this machine's address (default: auto-detected)
#   DOZ_PXE=none|proxy|range|external
#                              none (default) | proxy DHCP | DOZ_DHCP_RANGE=A,B |
#                              external: your DHCP server points PXE clients at this VM
#   DOZ_ADMIN_EMAIL=you@x      admin login (default: admin@example.com)
#   DOZ_DOMAIN=portal.example.com   portal domain: HTTPS and customer KVM
#   DOZ_CLOUDFLARE_TOKEN=...   Cloudflare API token (DNS edit) for the certificate
#   DOZ_SIM=0                  do not start the BMC simulator
#   DOZ_BRANCH=<name>          which branch to install (default: the repository's default)
#   DOZ_REPO=<url or path>     where to get the code
#
#   DOZ_PXE=range DOZ_DHCP_RANGE=10.0.0.200,10.0.0.249 curl -fsSL ... | sudo bash

set -euo pipefail

REPO="${DOZ_REPO:-https://github.com/ehab3233/DedicatedOZ.git}"
SRC=/opt/doz-src
PXE="${DOZ_PXE:-none}"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo: curl -fsSL <url> | sudo bash"
[ -f /etc/os-release ] && . /etc/os-release
[ "${ID:-}" = ubuntu ] || die "this installs on Ubuntu (found ${PRETTY_NAME:-unknown})"

export DEBIAN_FRONTEND=noninteractive
say "getting the code"
apt-get update -qq
apt-get install -y -qq --no-install-recommends git ca-certificates curl >/dev/null
BRANCH="${DOZ_BRANCH:-}"
if [ -z "$BRANCH" ]; then
    # Whatever the repository calls its default branch.
    BRANCH="$(git ls-remote --symref "$REPO" HEAD 2>/dev/null \
        | sed -n 's|^ref: refs/heads/\(.*\)[[:space:]]HEAD$|\1|p' | head -n1)"
    [ -n "$BRANCH" ] || die "could not read the default branch of $REPO; set DOZ_BRANCH"
fi
if [ -d "$SRC/.git" ]; then
    git -C "$SRC" fetch -q origin "$BRANCH"
    git -C "$SRC" checkout -q -B "$BRANCH" "origin/$BRANCH"
    echo "    updated $SRC to $BRANCH ($(git -C "$SRC" rev-parse --short HEAD))"
else
    rm -rf "$SRC"
    git clone -q --branch "$BRANCH" "$REPO" "$SRC"
    echo "    cloned $BRANCH into $SRC ($(git -C "$SRC" rev-parse --short HEAD))"
fi

ARGS=(--src "$SRC" --admin-email "${DOZ_ADMIN_EMAIL:-admin@example.com}")
[ -n "${DOZ_DOMAIN:-}" ] && ARGS+=(--domain "$DOZ_DOMAIN")
[ -n "${DOZ_CLOUDFLARE_TOKEN:-}" ] && ARGS+=(--cloudflare-token "$DOZ_CLOUDFLARE_TOKEN")
[ -n "${DOZ_IP:-}" ] && ARGS+=(--ip "$DOZ_IP")
case "$PXE" in
    none)  ARGS+=(--no-pxe) ;;
    proxy) ARGS+=(--proxy-dhcp) ;;
    external) ARGS+=(--external-dhcp) ;;
    range) [ -n "${DOZ_DHCP_RANGE:-}" ] || die "DOZ_PXE=range needs DOZ_DHCP_RANGE=START,END"
           ARGS+=(--dhcp-range "$DOZ_DHCP_RANGE") ;;
    *) die "DOZ_PXE must be none, proxy, range or external" ;;
esac

say "installing the management server"
# The installer prints its own summary, including the admin password on a
# first install. Keep its output; only its exit status is checked.
if ! "$SRC/deploy/install-management-server.sh" "${ARGS[@]}"; then
    die "the installer reported a problem; see the lines above"
fi

if [ "${DOZ_SIM:-1}" != 0 ]; then
    say "starting the BMC simulator (SIM-0001)"
    apt-get install -y -qq --no-install-recommends openipmi >/dev/null
    /opt/doz/doz.sh sim start
    /opt/doz/doz.sh sim events >/dev/null 2>&1 || true
    echo "    remove it later with: sudo /opt/doz/doz.sh sim remove"
fi

IP="$(sed -n 's/^MGMT_IP=//p' /etc/doz/install.conf 2>/dev/null || true)"
cat <<DONE

=======================================================================
 Done. Open http://${IP:-this-machine} and sign in with the admin login
 and password printed above (reset it with: sudo /opt/doz/doz.sh reset-admin EMAIL).

 Your servers' CIMCs need to be reachable from this machine (UDP 623 for
 IPMI, TCP 443 for Redfish/KVM tokens), and they must reach this machine
 on TCP 8080 to fetch ISO images for virtual-media installs. Your browser
 opens the KVM by talking to the CIMC directly on TCP 443.

 Update later by running this same command again.
=======================================================================
DONE
