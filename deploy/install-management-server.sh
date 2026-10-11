#!/usr/bin/env bash
# Install (or update) DedicatedOZ on an Ubuntu 22.04 / 24.04 VM, as a service.
#
# Everything runs under one systemd service, `doz`:
#
#   sudo systemctl status doz      # the umbrella
#   sudo systemctl restart doz     # restarts every part
#   ./doz.sh status                # each part, plus worker health
#
# Parts (all PartOf=doz.service, all started at boot):
#   doz-api                API behind nginx
#   doz-worker-power       power actions, Prepare BMC       (never waits on installs)
#   doz-worker-provision   reinstall / rescue / wipe        (long-running)
#   doz-worker-poll        inventory, health, sweeps
#   doz-beat               the scheduler
#   doz-pxe                DHCP/TFTP for netboot (dnsmasq)  (unless --no-pxe)
# plus PostgreSQL, Redis and nginx from Ubuntu.
#
# First install -- pick how PXE should get addresses on the flat network:
#
#   sudo ./deploy/install-management-server.sh --ip 10.0.0.5 --dhcp-range 10.0.0.200,10.0.0.249
#   sudo ./deploy/install-management-server.sh --ip 10.0.0.5 --proxy-dhcp
#   sudo ./deploy/install-management-server.sh --ip 10.0.0.5 --no-pxe
#
#   --dhcp-range   this VM becomes the DHCP server (nothing else hands out addresses)
#   --external-dhcp  your DHCP server (on the servers' VLAN) points PXE clients at
#                  this VM; this VM only serves TFTP and the boot scripts
#   --proxy-dhcp   your router keeps doing DHCP; this only adds the PXE options
#   --no-pxe       no DHCP/TFTP at all: panel, power and console only (add PXE later)
#   --domain D     the portal's hostname: HTTPS for the portal and customer KVM at
#                  kvm-<label>.D (needs A records for D and *.D pointing here)
#   --cloudflare-token T  Cloudflare API token (Zone.DNS edit) for the wildcard
#                  certificate; omitted, an existing /etc/letsencrypt cert is used
#
# Update: pull the new code and run it again with no arguments. The settings
# from the first run are kept in /etc/doz/install.conf; secrets in
# /etc/doz/doz.env are never overwritten.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/doz"
CONF=/etc/doz/install.conf

MGMT_IP=""
IFACE=""
PXE_MODE=""          # authoritative | proxy | none
DHCP_RANGE=""
ADMIN_EMAIL=""
DOMAIN=""            # portal hostname; with it, HTTPS and customer KVM
CLOUDFLARE_TOKEN=""  # Cloudflare API token for the wildcard certificate
FETCH_IMAGES=0
WITH_DOCKER=0

usage() { sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# Saved settings first, so arguments override them.
if [ -f "$CONF" ]; then
    # shellcheck disable=SC1090
    . "$CONF"
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --ip)           MGMT_IP="$2"; shift 2 ;;
        --iface)        IFACE="$2"; shift 2 ;;
        --dhcp-range)   PXE_MODE=authoritative; DHCP_RANGE="$2"; shift 2 ;;
        --proxy-dhcp)   PXE_MODE=proxy; DHCP_RANGE=""; shift ;;
        --no-pxe)       PXE_MODE=none; DHCP_RANGE=""; shift ;;
        --external-dhcp) PXE_MODE=external; DHCP_RANGE=""; shift ;;
        --admin-email)  ADMIN_EMAIL="$2"; shift 2 ;;
        --domain)       DOMAIN="$2"; shift 2 ;;
        --cloudflare-token) CLOUDFLARE_TOKEN="$2"; shift 2 ;;
        --fetch-images) FETCH_IMAGES=1; shift ;;
        --with-docker)  WITH_DOCKER=1; shift ;;
        --src)          SRC_DIR="$2"; shift 2 ;;
        -h|--help)      usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 2 ;;
    esac
done
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
# runuser, not sudo: minimal and cloud images do not always ship sudo, and
# the installer already runs as root.
as_doz()      { runuser -u doz -- "$@"; }
as_postgres() { runuser -u postgres -- "$@"; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
[ -f "$SRC_DIR/backend/pyproject.toml" ] || die "cannot find the repository at $SRC_DIR (use --src)"
[ -n "$PXE_MODE" ] || die "first install: choose --dhcp-range START,END, --proxy-dhcp, --external-dhcp or --no-pxe"

. /etc/os-release
[ "${ID:-}" = ubuntu ] || die "this installer targets Ubuntu (found ${PRETTY_NAME:-unknown})"
case "${VERSION_ID:-}" in
    22.04|24.04|26.04) ;;
    *)
        # Newer than this script knows: the package names below have been
        # stable across releases, so continue and say so. Older is refused.
        if [ "${VERSION_ID%%.*}" -ge 24 ] 2>/dev/null; then
            echo "note: Ubuntu ${VERSION_ID} is newer than this installer was tested on; continuing"
        else
            die "this installer needs Ubuntu 22.04 or newer (found ${PRETTY_NAME:-unknown})"
        fi ;;
esac

export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# Prerequisites for working out the network
# ---------------------------------------------------------------------------

say "preparing"
apt-get update -qq
apt-get install -y -qq --no-install-recommends iproute2 python3 ca-certificates curl gnupg >/dev/null

if [ -z "$IFACE" ]; then
    IFACE="$(ip -o route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -n1)"
    [ -n "$IFACE" ] || IFACE="$(ip -o link show | awk -F': ' '$2!="lo"{print $2; exit}' | cut -d@ -f1)"
fi
if [ -z "$MGMT_IP" ]; then
    MGMT_IP="$(ip -o -4 addr show dev "$IFACE" | awk '{print $4}' | cut -d/ -f1 | head -n1)"
fi
[ -n "$MGMT_IP" ] || die "could not determine this machine's IP; pass --ip"
CIDR="$(ip -o -4 addr show dev "$IFACE" | awk '{print $4}' | head -n1)"
[ -n "$CIDR" ] || die "interface $IFACE has no IPv4 address; pass --iface"
read -r NET_ADDR NET_MASK < <(python3 -c "import ipaddress,sys; n=ipaddress.ip_network(sys.argv[1], strict=False); print(n.network_address, n.netmask)" "$CIDR")
GATEWAY="$(ip -o -4 route show default 2>/dev/null | awk '{print $3}' | head -n1)"

say "DedicatedOZ management server"
note "OS:          $PRETTY_NAME"
note "interface:   $IFACE  ($CIDR)"
note "this host:   $MGMT_IP"
note "gateway:     ${GATEWAY:-none}"
case "$PXE_MODE" in
    authoritative) note "PXE:         this VM is the DHCP server, range $DHCP_RANGE" ;;
    proxy)         note "PXE:         proxy DHCP beside your existing DHCP server" ;;
    none)          note "PXE:         off (panel, power and console only)" ;;
    external)      note "PXE:         TFTP only; your DHCP server sends PXE clients here" ;;
esac
note "install to:  $INSTALL_DIR"

mkdir -p /etc/doz
cat > "$CONF" <<EOF
# Settings from the last run of install-management-server.sh. Re-running the
# installer with no arguments reuses these; arguments override them.
MGMT_IP=$MGMT_IP
IFACE=$IFACE
PXE_MODE=$PXE_MODE
DHCP_RANGE=$DHCP_RANGE
ADMIN_EMAIL=$ADMIN_EMAIL
DOMAIN=$DOMAIN
EOF

# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------

say "installing packages"
# dnsmasq-base, not dnsmasq: the full package brings its own service, which
# listens for DNS on port 53 (clashing with systemd-resolved) and registers
# itself as the host's resolver. We run dnsmasq under our own unit instead.
# Redis, or Valkey where a release has dropped Redis (Debian did; Ubuntu
# ships both for now). Same protocol, same port; only the unit name differs.
# (Captured into a variable, not piped into grep -q: under pipefail, grep -q
# closing the pipe early makes the whole pipeline report failure.)
apt_candidate() { apt-cache policy "$1" 2>/dev/null | sed -n 's/^ *Candidate: \([0-9].*\)$/\1/p'; }
if [ -n "$(apt_candidate redis-server)" ]; then
    REDIS_PKG=redis-server; REDIS_UNIT=redis-server
elif [ -n "$(apt_candidate valkey-server)" ]; then
    REDIS_PKG=valkey-server; REDIS_UNIT=valkey-server
else
    die "neither redis-server nor valkey-server is available from apt"
fi
apt-get install -y -qq --no-install-recommends \
    git rsync postgresql "$REDIS_PKG" nginx dnsmasq-base \
    ipmitool libarchive-tools iputils-ping >/dev/null
note "queue broker: $REDIS_PKG"
echo "REDIS_UNIT=$REDIS_UNIT" >> "$CONF"

# Python: the code needs 3.11+. 22.04 ships 3.10; 24.04 ships 3.12; 26.04
# ships 3.14, and every dependency has wheels for it (checked at install).
if [ "$VERSION_ID" = "22.04" ]; then
    if ! command -v python3.11 >/dev/null; then
        note "Ubuntu 22.04: adding deadsnakes for python3.11"
        apt-get install -y -qq --no-install-recommends software-properties-common >/dev/null
        add-apt-repository -y ppa:deadsnakes/ppa >/dev/null
        apt-get update -qq
    fi
    apt-get install -y -qq --no-install-recommends python3.11 python3.11-venv python3.11-dev >/dev/null
    PYTHON=python3.11
else
    apt-get install -y -qq --no-install-recommends python3-venv python3-dev >/dev/null
    PYTHON=python3
fi
note "python: $($PYTHON --version)"

# Node 22 builds the frontend. Nothing runs on it afterwards.
if ! command -v node >/dev/null || [ "$(node -v | cut -d. -f1 | tr -d v)" -lt 20 ]; then
    note "installing Node.js 22 (build-time only)"
    mkdir -p /etc/apt/keyrings
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list
    apt-get update -qq && apt-get install -y -qq nodejs >/dev/null
fi
note "node: $(node -v)"

if [ "$WITH_DOCKER" -eq 1 ] && ! command -v docker >/dev/null; then
    note "installing docker (for building the installer ramdisk)"
    apt-get install -y -qq docker.io >/dev/null
    systemctl enable --now docker >/dev/null
fi

# ---------------------------------------------------------------------------
# Upgrading from an earlier layout
# ---------------------------------------------------------------------------

if [ -f /etc/systemd/system/doz-worker.service ]; then
    note "replacing the single doz-worker service with per-queue workers"
    systemctl disable --now doz-worker.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/doz-worker.service
fi
if [ -f /etc/dnsmasq.d/doz.conf ]; then
    note "moving PXE from the distro dnsmasq service to doz-pxe"
    rm -f /etc/dnsmasq.d/doz.conf
    systemctl disable --now dnsmasq.service >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------------------
# User and files
# ---------------------------------------------------------------------------

say "installing to $INSTALL_DIR"
id doz >/dev/null 2>&1 || useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin doz
mkdir -p "$INSTALL_DIR" /etc/doz/cimc /var/lib/doz "$INSTALL_DIR/installer/assets/iso" \
         "$INSTALL_DIR/installer/tftp"

if [ "$(readlink -f "$SRC_DIR")" != "$(readlink -f "$INSTALL_DIR")" ]; then
    rsync -a --delete \
        --exclude '.venv' --exclude 'node_modules' --exclude '.run' --exclude '.git' \
        --exclude 'frontend/dist' --exclude 'installer/assets' --exclude 'installer/tftp' \
        --exclude '.env' \
        "$SRC_DIR/" "$INSTALL_DIR/"
fi
chown -R doz:doz "$INSTALL_DIR" /var/lib/doz
chown root:doz /etc/doz; chmod 750 /etc/doz
chown doz:doz /etc/doz/cimc; chmod 700 /etc/doz/cimc

# ---------------------------------------------------------------------------
# PostgreSQL and Redis
# ---------------------------------------------------------------------------

say "database"
systemctl enable --now postgresql "$REDIS_UNIT" >/dev/null
for _ in $(seq 1 30); do as_postgres psql -qtAc "SELECT 1" >/dev/null 2>&1 && break; sleep 1; done
if [ -f /etc/doz/doz.env ] && grep -q '^DOZ_DATABASE_URL=' /etc/doz/doz.env; then
    DB_PASS="$(sed -n 's|^DOZ_DATABASE_URL=postgresql+psycopg://doz:\([^@]*\)@.*|\1|p' /etc/doz/doz.env)"
    note "keeping existing database credentials"
else
    DB_PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
fi
if ! as_postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='doz'" | grep -q 1; then
    as_postgres psql -qc "CREATE USER doz WITH PASSWORD '$DB_PASS';"
else
    as_postgres psql -qc "ALTER USER doz WITH PASSWORD '$DB_PASS';"
fi
as_postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='doz'" | grep -q 1 \
    || as_postgres createdb -O doz doz
note "postgres role and database ready"

# ---------------------------------------------------------------------------
# Environment file
# ---------------------------------------------------------------------------

say "configuration"
if [ "$PXE_MODE" = "authoritative" ]; then PIN=true; else PIN=false; fi
if [ ! -f /etc/doz/doz.env ]; then
    JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    cat > /etc/doz/doz.env <<ENV
# DedicatedOZ production configuration. Read by every doz-* service.
# Change something, then: sudo systemctl restart doz
DOZ_ENVIRONMENT=production
DOZ_LOG_LEVEL=INFO

DOZ_DATABASE_URL=postgresql+psycopg://doz:${DB_PASS}@127.0.0.1:5432/doz
DOZ_REDIS_URL=redis://127.0.0.1:6379/0

DOZ_JWT_SECRET=${JWT_SECRET}
DOZ_ACCESS_TOKEN_TTL_SECONDS=28800

# Where machines being installed reach us. Both go through nginx.
DOZ_CONTROL_PLANE_URL=http://${MGMT_IP}
DOZ_BOOT_ASSET_BASE_URL=http://${MGMT_IP}:8080

# CIMC passwords live in /etc/doz/cimc/<ref>.json, written from the panel.
DOZ_SECRETS_BACKEND=file
DOZ_SECRETS_FILE_DIR=/etc/doz/cimc

# How power and boot are driven: auto (IPMI, then Redfish), ipmi, redfish.
DOZ_BMC_PROTOCOL=auto
# ipmitool: pin cipher suite 3, or every call can cost ten seconds.
DOZ_IPMI_CIPHER_SUITE=3
DOZ_REDFISH_VERIFY_TLS=false
DOZ_REDFISH_TIMEOUT_SECONDS=30
DOZ_REDFISH_MAX_RETRIES=3

DOZ_INSTALL_TIMEOUT_SECONDS=2700
DOZ_CALLBACK_TOKEN_TTL_SECONDS=14400
DOZ_REQUIRE_WIPE_BEFORE_STOCK=true
# ${PIN}: authoritative dnsmasq keys leases on MAC (safe to pin); with proxy
# DHCP the installer may not get the address iPXE did.
DOZ_BOOT_PIN_CLIENT_IP=${PIN}

# nginx is in front of the API and sets X-Forwarded-For.
DOZ_TRUST_PROXY_HEADERS=true
DOZ_CORS_ORIGINS=http://${MGMT_IP}${DOMAIN:+,https://$DOMAIN}
# The portal's domain: HTTPS for the portal, and the customer graphical
# console at kvm-<label>.<domain>, proxied by nginx to the server's CIMC.
DOZ_PORTAL_DOMAIN=${DOMAIN}
DOZ_PORTAL_URL=${DOMAIN:+https://$DOMAIN}
DOZ_KVM_GRANT_HOURS=4

DOZ_INSTALLER_TEMPLATE_DIR=${INSTALL_DIR}/installer/templates
# What nginx serves at http://${MGMT_IP}:8080/: ramdisk, kernels, initrds, ISOs.
DOZ_BOOT_ASSET_DIR=${INSTALL_DIR}/installer/assets
# ISO image store, served to BMCs at http://${MGMT_IP}:8080/iso/
DOZ_IMAGE_DIR=${INSTALL_DIR}/installer/assets/iso
# Email (optional). With an SMTP host, customers are mailed when a reinstall,
# rescue boot or wipe on their server finishes. PORTAL_URL is what they open.
#DOZ_SMTP_HOST=smtp.example.com
#DOZ_SMTP_PORT=587
#DOZ_SMTP_USERNAME=
#DOZ_SMTP_PASSWORD=
#DOZ_MAIL_FROM=noreply@example.com
#DOZ_PORTAL_URL=https://portal.example.com
# Router automation (optional). With a RouterOS 7 REST URL and a user that has
# the read, write, api and rest-api policies, assigning a server's primary
# address and every install put its switch port into the block's VLAN and
# add a PXE lease for its MAC. Empty: the Network tab shows the commands.
#DOZ_ROUTEROS_URL=https://10.20.0.1/rest
#DOZ_ROUTEROS_USERNAME=doz
#DOZ_ROUTEROS_PASSWORD=
#DOZ_ROUTEROS_BRIDGE=bridge
# Prepare BMC: persistent boot order it sets (disk,pxe | pxe,disk | empty = leave)
# and NTP servers it points each CIMC at (comma-separated; empty = leave).
DOZ_BMC_PREPARE_BOOT_ORDER=disk,pxe
# and the BIOS power profile: balanced | low_power | performance | empty = leave.
DOZ_BMC_PREPARE_POWER_PROFILE=balanced
DOZ_NTP_SERVERS=
DOZ_IPMITOOL_PATH=/usr/bin/ipmitool
DOZ_SOL_IDLE_TIMEOUT_SECONDS=1800
# vKVM: empty probes the CIMC for its HTML5 viewer. Placeholders {host} {tkn1} {tkn2}.
# DOZ_KVM_URL_TEMPLATE='https://{host}/html/kvmViewer.html?tkn1={tkn1}&tkn2={tkn2}'
ENV
    note "wrote /etc/doz/doz.env"
else
    note "keeping existing /etc/doz/doz.env"
fi
chown root:doz /etc/doz/doz.env; chmod 640 /etc/doz/doz.env

# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

say "backend"
as_doz bash -c "
    set -e
    cd '$INSTALL_DIR/backend'
    [ -x .venv/bin/python ] || $PYTHON -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -e .
"
note "python dependencies installed"

INIT_OUT="$(as_doz bash -c "set -a; . /etc/doz/doz.env; set +a; cd '$INSTALL_DIR/backend' && .venv/bin/python -m scripts.init_db --admin-email '$ADMIN_EMAIL'")"
echo "$INIT_OUT" | sed 's/^/    /'
ADMIN_PASSWORD="$(echo "$INIT_OUT" | sed -n 's/^admin password: *//p')"

# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

say "frontend"
as_doz bash -c "cd '$INSTALL_DIR/frontend' && npm ci --silent --no-audit --no-fund && npm run build --silent" >/dev/null
note "built to $INSTALL_DIR/frontend/dist"

# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------

say "services"
VENV="$INSTALL_DIR/backend/.venv/bin"
CELERY="$VENV/celery -A app.workers.celery_app.celery_app"

part() {
    # part <name> <description> <exec> [extra [Service] lines...]
    local name="$1" description="$2" exec="$3"; shift 3
    {
        cat <<UNIT
[Unit]
Description=DedicatedOZ $description
PartOf=doz.service
After=network-online.target postgresql.service ${REDIS_UNIT}.service
Wants=network-online.target

[Service]
Type=simple
User=doz
Group=doz
WorkingDirectory=$INSTALL_DIR/backend
EnvironmentFile=/etc/doz/doz.env
ExecStart=$exec
Restart=always
RestartSec=3
# Workers hold BMC credentials in memory; keep them locked down.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/etc/doz/cimc /var/lib/doz $INSTALL_DIR/installer/assets
UNIT
        for line in "$@"; do echo "$line"; done
        cat <<UNIT

[Install]
WantedBy=doz.service
UNIT
    } > "/etc/systemd/system/$name.service"
}

part doz-api "API" \
    "$VENV/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1"
# One worker per queue, so a pile of twenty-minute reinstalls can never sit in
# front of someone's power button.
part doz-worker-power "power worker" \
    "$CELERY worker -n power@%%h -Q power --concurrency=8 --loglevel=info" \
    "KillMode=mixed" "TimeoutStopSec=60"
part doz-worker-provision "provisioning worker" \
    "$CELERY worker -n provision@%%h -Q provision --concurrency=8 --loglevel=info" \
    "KillMode=mixed" "TimeoutStopSec=60"
part doz-worker-poll "polling worker" \
    "$CELERY worker -n poll@%%h -Q poll --concurrency=4 --loglevel=info" \
    "KillMode=mixed" "TimeoutStopSec=60"
part doz-beat "scheduler" \
    "$CELERY beat --loglevel=info --schedule /var/lib/doz/celerybeat-schedule"

PARTS="doz-api doz-worker-power doz-worker-provision doz-worker-poll doz-beat"

if [ "$PXE_MODE" != "none" ]; then
    cat > /etc/systemd/system/doz-pxe.service <<UNIT
[Unit]
Description=DedicatedOZ PXE (DHCP/TFTP via dnsmasq)
PartOf=doz.service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStartPre=/usr/sbin/dnsmasq --test --conf-file=/etc/doz/dnsmasq.conf
ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --conf-file=/etc/doz/dnsmasq.conf --pid-file=/run/doz-pxe.pid
Restart=on-failure
RestartSec=3

[Install]
WantedBy=doz.service
UNIT
    PARTS="$PARTS doz-pxe"
else
    if [ -f /etc/systemd/system/doz-pxe.service ]; then
        systemctl disable --now doz-pxe.service >/dev/null 2>&1 || true
        rm -f /etc/systemd/system/doz-pxe.service
    fi
fi

WANTS=""
for p in $PARTS; do WANTS="$WANTS $p.service"; done
cat > /etc/systemd/system/doz.service <<UNIT
[Unit]
Description=DedicatedOZ control plane
Documentation=file://$INSTALL_DIR/docs/GETTING-STARTED.md
Wants=$WANTS
After=postgresql.service ${REDIS_UNIT}.service

# The umbrella: start, stop and restart it and every part follows (each part
# is PartOf=doz.service). The work happens in the parts; this unit only
# groups them, the same pattern Ubuntu uses for openvpn.service.
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/true
ExecReload=/bin/true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
# shellcheck disable=SC2086
systemctl enable doz.service $WANTS >/dev/null 2>&1
note "doz.service enabled at boot, with:$WANTS"

# ---------------------------------------------------------------------------
# nginx
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Certificate for the portal's domain and kvm-*.<domain> (Let's Encrypt,
# DNS validation through Cloudflare, so the wildcard is covered).
# ---------------------------------------------------------------------------

CERT_DIR=""
if [ -n "$DOMAIN" ]; then
    say "certificate for $DOMAIN and *.$DOMAIN"
    apt-get install -y -qq --no-install-recommends certbot python3-certbot-dns-cloudflare >/dev/null
    if [ -n "$CLOUDFLARE_TOKEN" ]; then
        umask 077
        printf 'dns_cloudflare_api_token = %s\n' "$CLOUDFLARE_TOKEN" > /etc/doz/cloudflare.ini
        umask 022
    fi
    if [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
        note "certificate present; certbot's timer renews it"
        CERT_DIR="/etc/letsencrypt/live/$DOMAIN"
    elif [ -f /etc/doz/cloudflare.ini ]; then
        if certbot certonly --dns-cloudflare --dns-cloudflare-credentials /etc/doz/cloudflare.ini \
                --dns-cloudflare-propagation-seconds 30 -d "$DOMAIN" -d "*.$DOMAIN" \
                --non-interactive --agree-tos -m "$ADMIN_EMAIL" >/tmp/certbot.log 2>&1; then
            CERT_DIR="/etc/letsencrypt/live/$DOMAIN"
            note "issued; certbot's timer renews it"
        else
            note "certbot failed (see /tmp/certbot.log); the portal stays on HTTP at $MGMT_IP"
            FAILED="$FAILED certificate"
        fi
    else
        note "no Cloudflare token (--cloudflare-token) and no certificate yet: HTTP only for now"
    fi
    mkdir -p /etc/letsencrypt/renewal-hooks/deploy
    cat > /etc/letsencrypt/renewal-hooks/deploy/doz-nginx.sh <<'HOOK'
#!/bin/sh
systemctl reload nginx
HOOK
    chmod 755 /etc/letsencrypt/renewal-hooks/deploy/doz-nginx.sh
fi

say "nginx"
cat > /etc/nginx/sites-available/doz <<NGINX
# DedicatedOZ portal + API on :80, boot assets on :8080.

map \$http_upgrade \$connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80 default_server;
    server_name _;
    root $INSTALL_DIR/frontend/dist;
    index index.html;
    client_max_body_size 16m;

    # The React app; unknown paths fall through to it for client-side routing.
    location / {
        try_files \$uri /index.html;
    }

    # ISO uploads: multi-gigabyte bodies, streamed straight through to the
    # API rather than buffered on nginx's disk first.
    location = /api/v1/admin/images/upload {
        client_max_body_size 0;
        proxy_request_buffering off;
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # API, netboot rail, docs. The upgrade headers carry the serial console
    # websocket; the long timeout keeps an idle console open.
    location ~ ^/(api|boot|health|docs|openapi\.json|redoc) {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_set_header   Upgrade           \$http_upgrade;
        proxy_set_header   Connection        \$connection_upgrade;
        proxy_read_timeout 3600s;
        proxy_buffering    off;
    }
}

$( if [ -n "$CERT_DIR" ]; then
DOMAIN_RE="$(printf '%s' "$DOMAIN" | sed 's/\./\\./g')"
cat <<TLS
# The portal on its domain, over TLS. Plain HTTP on the name redirects; the
# address stays on plain HTTP above because the netboot rail needs it.
server {
    listen 80;
    server_name $DOMAIN;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    http2 on;
    server_name $DOMAIN;
    ssl_certificate     $CERT_DIR/fullchain.pem;
    ssl_certificate_key $CERT_DIR/privkey.pem;
    root $INSTALL_DIR/frontend/dist;
    index index.html;
    client_max_body_size 16m;
    location / {
        try_files \$uri /index.html;
    }
    location = /api/v1/admin/images/upload {
        client_max_body_size 0;
        proxy_request_buffering off;
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
    location ~ ^/(api|boot|health|docs|openapi\.json|redoc) {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_set_header   Upgrade           \$http_upgrade;
        proxy_set_header   Connection        \$connection_upgrade;
        proxy_read_timeout 3600s;
        proxy_buffering    off;
    }
}

# Customer graphical console: kvm-<label>.$DOMAIN is one server's CIMC, for
# a browser the API says may see it. nginx asks the API on every request
# (auth_request) and proxies to the CIMC the answer names, websockets and
# all, so the CIMC's own viewer runs unchanged.
server {
    listen 443 ssl;
    http2 on;
    server_name ~^kvm-[a-z0-9]+\\.${DOMAIN_RE}\$;
    ssl_certificate     $CERT_DIR/fullchain.pem;
    ssl_certificate_key $CERT_DIR/privkey.pem;

    location = /__doz_kvm_auth {
        internal;
        proxy_pass               http://127.0.0.1:8000/api/v1/kvm/authorize;
        proxy_pass_request_body  off;
        proxy_set_header         Content-Length "";
        proxy_set_header         X-KVM-Host \$host;
        proxy_set_header         Cookie \$http_cookie;
    }

    location / {
        auth_request      /__doz_kvm_auth;
        auth_request_set  \$kvm_upstream      \$upstream_http_x_upstream;
        auth_request_set  \$kvm_upstream_host \$upstream_http_x_upstream_host;
        error_page 401 = @no_access;

        proxy_pass              \$kvm_upstream;
        proxy_ssl_verify        off;
        proxy_ssl_server_name   off;
        proxy_http_version      1.1;
        proxy_set_header        Host              \$kvm_upstream_host;
        proxy_set_header        Upgrade           \$http_upgrade;
        proxy_set_header        Connection        \$connection_upgrade;
        proxy_set_header        X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_redirect          ~^https?://[^/]+(/.*)\$ https://\$host\$1;
        proxy_cookie_domain     ~.* \$host;
        proxy_read_timeout      3600s;
        proxy_send_timeout      3600s;
        proxy_buffering         off;
        client_max_body_size    0;
    }

    location @no_access {
        default_type text/plain;
        return 401 "No console access for this address. Open it from the portal's Console tab.\n";
    }
}
TLS
fi )

# Boot assets: kernels, initrds, and the ISO image store under /iso/. Plain
# HTTP, no redirects -- the CIMC's virtual media cannot follow one and cannot
# validate our certificate.
server {
    listen 8080;
    server_name _;
    root $INSTALL_DIR/installer/assets;
    autoindex on;
    sendfile on;
    tcp_nopush on;
    location / {
        try_files \$uri \$uri/ =404;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/doz /etc/nginx/sites-enabled/doz
rm -f /etc/nginx/sites-enabled/default
nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx config failed validation"; }
systemctl enable nginx >/dev/null 2>&1
systemctl restart nginx
note "portal on http://$MGMT_IP, boot assets on http://$MGMT_IP:8080"

# ---------------------------------------------------------------------------
# PXE config
# ---------------------------------------------------------------------------

if [ "$PXE_MODE" != "none" ]; then
    say "PXE"
    TFTP="$INSTALL_DIR/installer/tftp"
    # BIOS and UEFI iPXE builds. boot.ipxe.org keeps the EFI one under
    # x86_64-efi/ (the old top-level ipxe.efi URL now 404s).
    for pair in "undionly.kpxe=undionly.kpxe" "ipxe.efi=x86_64-efi/ipxe.efi"; do
        f="${pair%%=*}"; path="${pair#*=}"
        if [ ! -s "$TFTP/$f" ]; then
            note "fetching $f from boot.ipxe.org"
            curl -fsSL --retry 3 -o "$TFTP/$f.part" "https://boot.ipxe.org/$path" \
                && mv "$TFTP/$f.part" "$TFTP/$f" \
                || { rm -f "$TFTP/$f.part"; note "could not fetch $f; place it in $TFTP by hand"; }
        fi
    done
    # Loaders with the control plane URL built in: stock iPXE asks DHCP for a
    # filename again and, on a DHCP server that cannot tell it from the PXE
    # ROM (MikroTik, most routers), loops on undionly.kpxe. Built from
    # source in docker, so only when docker is here; doz.sh ramdisk and
    # doz.sh ipxe build them otherwise. Rebuilt when the address changes or
    # the script built into them does.
    WANT_URL="http://${MGMT_IP}"
    WANT_EMBED="$(bash "$INSTALL_DIR/installer/build-ipxe.sh" --url "$WANT_URL" --print-embed 2>/dev/null || true)"
    HAVE_EMBED="$(cat "$TFTP/.embedded.ipxe" 2>/dev/null || true)"
    if [ "$HAVE_EMBED" != "$WANT_EMBED" ]; then
        if command -v docker >/dev/null 2>&1; then
            note "building iPXE loaders that chain to $WANT_URL (a few minutes)"
            bash "$INSTALL_DIR/installer/build-ipxe.sh" --url "$WANT_URL" --out "$TFTP" >/dev/null 2>&1 \
                || note "iPXE build failed; run sudo $INSTALL_DIR/doz.sh ipxe to see why"
        else
            note "stock iPXE loaders in place; before the first reinstall run"
            note "  sudo $INSTALL_DIR/doz.sh ramdisk   (builds the ramdisk and loaders that chain to $WANT_URL)"
        fi
    fi
    chown -R doz:doz "$TFTP"
    # dnsmasq drops to its own user and refuses to start ("TFTP directory
    # inaccessible") unless it can walk the whole path; a strict umask on
    # the first install would otherwise leave /opt/doz unreadable to it.
    chmod 755 "$INSTALL_DIR" "$INSTALL_DIR/installer" "$TFTP"
    chmod -R a+rX "$TFTP"
    DNSMASQ_USER=nobody
    id dnsmasq >/dev/null 2>&1 && DNSMASQ_USER=dnsmasq

    cat > /etc/doz/dnsmasq.conf <<DNSMASQ
# DedicatedOZ PXE, run by doz-pxe.service. Generated by the installer.
port=0
interface=$IFACE
bind-dynamic
user=$DNSMASQ_USER
log-facility=-
log-dhcp

enable-tftp
tftp-root=$TFTP

# The NIC's PXE ROM gets iPXE over TFTP; iPXE (user class "iPXE", option 175)
# gets the control plane URL and never touches TFTP again.
dhcp-match=set:ipxe,175
dhcp-match=set:efi64,option:client-arch,7
dhcp-match=set:efi64,option:client-arch,9
DNSMASQ

    if [ "$PXE_MODE" = "authoritative" ]; then
        cat >> /etc/doz/dnsmasq.conf <<DNSMASQ

# Authoritative DHCP for the flat network.
dhcp-range=${DHCP_RANGE},${NET_MASK},12h
dhcp-authoritative
${GATEWAY:+dhcp-option=option:router,$GATEWAY}
dhcp-option=option:dns-server,1.1.1.1,8.8.8.8
# Key leases on MAC only: iPXE, the installer ramdisk and the OS installer each
# send a different client-id and must all land on the same address.
dhcp-ignore-clid

dhcp-boot=tag:!ipxe,tag:!efi64,undionly.kpxe,,${MGMT_IP}
dhcp-boot=tag:!ipxe,tag:efi64,ipxe.efi,,${MGMT_IP}
dhcp-boot=tag:ipxe,http://${MGMT_IP}/boot/ipxe
DNSMASQ
    elif [ "$PXE_MODE" = "proxy" ]; then
        cat >> /etc/doz/dnsmasq.conf <<DNSMASQ

# Proxy DHCP: the existing DHCP server keeps handing out addresses; this only
# adds PXE boot information alongside its replies.
dhcp-range=${NET_ADDR},proxy,${NET_MASK}
pxe-prompt="DedicatedOZ",1
pxe-service=tag:!ipxe,x86PC,"Boot DedicatedOZ (BIOS)",undionly.kpxe
pxe-service=tag:!ipxe,X86-64_EFI,"Boot DedicatedOZ (UEFI)",ipxe.efi
pxe-service=tag:ipxe,x86PC,"DedicatedOZ",http://${MGMT_IP}/boot/ipxe
pxe-service=tag:ipxe,X86-64_EFI,"DedicatedOZ",http://${MGMT_IP}/boot/ipxe
DNSMASQ
    else
        cat >> /etc/doz/dnsmasq.conf <<DNSMASQ

# External DHCP: no DHCP here at all. The DHCP server on the servers' VLAN
# hands out next-server=${MGMT_IP} with undionly.kpxe (BIOS) or ipxe.efi
# (UEFI); the loaders served here chain to http://${MGMT_IP}/boot/ipxe on
# their own. This dnsmasq only serves TFTP.
DNSMASQ
    fi
    /usr/sbin/dnsmasq --test --conf-file=/etc/doz/dnsmasq.conf >/dev/null 2>&1 \
        || { /usr/sbin/dnsmasq --test --conf-file=/etc/doz/dnsmasq.conf; die "dnsmasq config failed validation"; }
    note "doz-pxe will serve TFTP from $TFTP"
fi

# ---------------------------------------------------------------------------
# Firewall
# ---------------------------------------------------------------------------

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    say "firewall"
    for rule in 22/tcp 80/tcp 8080/tcp 67/udp 69/udp 4011/udp; do ufw allow "$rule" >/dev/null; done
    note "opened 22, 80, 8080/tcp and 67, 69, 4011/udp"
fi

# ---------------------------------------------------------------------------
# Optional: images and ramdisk
# ---------------------------------------------------------------------------

if [ "$FETCH_IMAGES" -eq 1 ]; then
    say "OS installer images (this downloads ~2.5 GB)"
    as_doz "$INSTALL_DIR/deploy/fetch-os-images.sh" --assets "$INSTALL_DIR/installer/assets"
fi

if [ "$WITH_DOCKER" -eq 1 ]; then
    say "installer ramdisk"
    bash "$INSTALL_DIR/installer/build-ramdisk.sh" --out "$INSTALL_DIR/installer/assets/doz-installer" || \
        note "ramdisk build failed; see installer/README.md"
    chown -R doz:doz "$INSTALL_DIR/installer/assets"
fi

# ---------------------------------------------------------------------------
# Start, and prove it
# ---------------------------------------------------------------------------

say "starting"
systemctl restart doz.service
FAILED=""
for p in $PARTS; do
    ok=0
    # A provisioning worker in the middle of a job gets up to TimeoutStopSec
    # (60 s) to stop before it restarts, so allow for that; give up early
    # only on a unit that has actually failed.
    for _ in $(seq 1 90); do
        state="$(systemctl is-active "$p" 2>/dev/null || true)"
        [ "$state" = active ] && { ok=1; break; }
        [ "$state" = failed ] && break
        sleep 1
    done
    if [ "$ok" -eq 1 ]; then note "$p: running"; else note "$p: NOT running"; FAILED="$FAILED $p"; fi
done

HEALTH=""
for _ in $(seq 1 30); do
    if HEALTH="$(curl -fsS http://127.0.0.1/health 2>/dev/null)"; then break; fi
    sleep 1
done
case "$HEALTH" in
    *'"status":"ok"'*) note "API healthy through nginx" ;;
    *) note "API not answering through nginx yet"; FAILED="$FAILED api-health" ;;
esac

# Workers take a few seconds after their unit starts to reach the broker.
WORKERS=0
for _ in $(seq 1 12); do
    WORKERS="$(as_doz bash -c "set -a; . /etc/doz/doz.env; set +a; cd '$INSTALL_DIR/backend' && $CELERY inspect ping --timeout 3" 2>/dev/null | grep -c 'pong' || true)"
    [ "$WORKERS" -ge 3 ] && break
    sleep 3
done
note "celery workers answering: $WORKERS of 3"
[ "$WORKERS" -ge 3 ] || FAILED="$FAILED workers"

cat <<SUMMARY

=======================================================================
 DedicatedOZ is installed and running as the 'doz' service.
=======================================================================

 Portal:        http://${MGMT_IP}${CERT_DIR:+  and https://$DOMAIN}
 API docs:      http://${MGMT_IP}/docs
 Boot assets:   http://${MGMT_IP}:8080
 Admin login:   ${ADMIN_EMAIL}
SUMMARY
if [ -n "$ADMIN_PASSWORD" ]; then
    echo " Password:      ${ADMIN_PASSWORD}"
    echo "                (shown once; reset with: $INSTALL_DIR/doz.sh reset-admin ${ADMIN_EMAIL})"
else
    echo " Password:      unchanged (account already existed)"
fi
cat <<SUMMARY

 Service:       sudo systemctl status|restart|stop doz
 Details:       $INSTALL_DIR/doz.sh status
 Logs:          $INSTALL_DIR/doz.sh logs          (journalctl -u 'doz*')
 Config:        /etc/doz/doz.env    (then: sudo systemctl restart doz)
 CIMC secrets:  /etc/doz/cimc/
 Update:        git pull && sudo ./doz.sh update

 No hardware yet? Try power and the serial console against a simulated BMC:
                sudo apt install openipmi && sudo $INSTALL_DIR/doz.sh sim start

 Before the first reinstall:
   1. sudo -u doz $INSTALL_DIR/deploy/fetch-os-images.sh   (or --fetch-images)
   2. sudo $INSTALL_DIR/doz.sh ramdisk                     (installs docker if needed)
   Both show on the panel's Images page under Netboot images.
   3. docs/GETTING-STARTED.md for CIMC setup and the first server
SUMMARY

if [ "$PXE_MODE" = "external" ]; then
cat <<EXTERNAL

 PXE with your own DHCP server: on the DHCP server for the servers' VLAN,
 for that subnet, set
   next-server (option 66) = ${MGMT_IP}
   filename    (option 67) = undionly.kpxe      (ipxe.efi for UEFI servers)
 That is all: the loaders served here chain to http://${MGMT_IP}/boot/ipxe
 themselves, whatever filename DHCP hands them afterwards.
 MikroTik RouterOS 7:
   /ip dhcp-server network set [find] next-server=${MGMT_IP} boot-file-name=undionly.kpxe
 The servers' VLAN must reach this VM on UDP 69 and TCP 80 and 8080.
EXTERNAL
fi

if [ -n "$FAILED" ]; then
    echo
    echo " WARNING: not everything came up:$FAILED"
    echo "          journalctl -u 'doz*' --since '-5min' shows why."
    exit 1
fi
