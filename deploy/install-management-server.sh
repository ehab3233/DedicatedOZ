#!/usr/bin/env bash
# Install DedicatedOZ on a fresh Ubuntu VM as the management server.
#
# Turns a bare Ubuntu 22.04 or 24.04 machine into the control plane:
#
#   PostgreSQL + Redis          state and queue
#   doz-api / worker / beat     systemd services running as the `doz` user
#   nginx                       portal on :80, boot assets on :8080
#   dnsmasq                     PXE (TFTP + DHCP or proxy-DHCP), no DNS
#   iPXE                        undionly.kpxe / ipxe.efi in the TFTP root
#
# Run from a checkout of the repository, as root:
#
#   sudo ./deploy/install-management-server.sh --ip 10.0.0.5 --dhcp-range 10.0.0.150,10.0.0.199
#   sudo ./deploy/install-management-server.sh --ip 10.0.0.5 --proxy-dhcp
#
# --dhcp-range makes this VM the DHCP server for the flat network (use it when
# nothing else is handing out addresses). --proxy-dhcp leaves your existing
# DHCP alone and only adds the PXE options alongside it. Pick one.
#
# Re-runnable: it skips what is already done and never overwrites an existing
# /etc/doz/doz.env, so secrets survive a re-run.

set -euo pipefail

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

MGMT_IP=""
IFACE=""
DHCP_RANGE=""
PROXY_DHCP=0
ADMIN_EMAIL="admin@example.com"
FETCH_IMAGES=0
WITH_DOCKER=0
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/doz"

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --ip)           MGMT_IP="$2"; shift 2 ;;
        --iface)        IFACE="$2"; shift 2 ;;
        --dhcp-range)   DHCP_RANGE="$2"; shift 2 ;;
        --proxy-dhcp)   PROXY_DHCP=1; shift ;;
        --admin-email)  ADMIN_EMAIL="$2"; shift 2 ;;
        --fetch-images) FETCH_IMAGES=1; shift ;;
        --with-docker)  WITH_DOCKER=1; shift ;;
        --src)          SRC_DIR="$2"; shift 2 ;;
        -h|--help)      usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 2 ;;
    esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
[ -f "$SRC_DIR/backend/pyproject.toml" ] || die "cannot find the repository at $SRC_DIR (use --src)"
[ -n "$DHCP_RANGE" ] || [ "$PROXY_DHCP" -eq 1 ] || die "choose --dhcp-range START,END or --proxy-dhcp"
[ -z "$DHCP_RANGE" ] || [ "$PROXY_DHCP" -eq 0 ] || die "--dhcp-range and --proxy-dhcp are mutually exclusive"

. /etc/os-release
case "${VERSION_ID:-}" in
    22.04|24.04) ;;
    *) die "this installer targets Ubuntu 22.04 or 24.04 (found ${PRETTY_NAME:-unknown})" ;;
esac

# ---------------------------------------------------------------------------
# Network facts
# ---------------------------------------------------------------------------

if [ -z "$IFACE" ]; then
    IFACE="$(ip -o route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -n1)"
    [ -n "$IFACE" ] || IFACE="$(ip -o link show | awk -F': ' '$2!="lo"{print $2; exit}')"
fi
if [ -z "$MGMT_IP" ]; then
    MGMT_IP="$(ip -o -4 addr show dev "$IFACE" | awk '{print $4}' | cut -d/ -f1 | head -n1)"
fi
[ -n "$MGMT_IP" ] || die "could not determine this machine's IP; pass --ip"
CIDR="$(ip -o -4 addr show dev "$IFACE" | awk '{print $4}' | head -n1)"
SUBNET="$(python3 -c "import ipaddress,sys; n=ipaddress.ip_network(sys.argv[1], strict=False); print(n.network_address, n.netmask)" "$CIDR")"
NET_ADDR="${SUBNET% *}"; NET_MASK="${SUBNET#* }"
GATEWAY="$(ip -o -4 route show default dev "$IFACE" 2>/dev/null | awk '{print $3}' | head -n1)"

say "DedicatedOZ management server"
note "OS:          $PRETTY_NAME"
note "interface:   $IFACE  ($CIDR)"
note "this host:   $MGMT_IP"
note "gateway:     ${GATEWAY:-none}"
if [ -n "$DHCP_RANGE" ]; then note "DHCP:        authoritative, range $DHCP_RANGE"; else note "DHCP:        proxy (existing DHCP server stays in charge)"; fi
note "install to:  $INSTALL_DIR"

# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------

say "installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    ca-certificates curl gnupg git rsync \
    postgresql redis-server nginx dnsmasq \
    ipmitool libarchive-tools \
    ufw >/dev/null

# Python: the code needs 3.11+. 24.04 ships 3.12; 22.04 ships 3.10.
if [ "$VERSION_ID" = "22.04" ]; then
    if ! command -v python3.11 >/dev/null; then
        note "Ubuntu 22.04: adding deadsnakes for python3.11"
        apt-get install -y -qq software-properties-common >/dev/null
        add-apt-repository -y ppa:deadsnakes/ppa >/dev/null
        apt-get update -qq
    fi
    apt-get install -y -qq python3.11 python3.11-venv python3.11-dev >/dev/null
    PYTHON=python3.11
else
    apt-get install -y -qq python3 python3-venv python3-dev >/dev/null
    PYTHON=python3
fi
note "python: $($PYTHON --version)"

# Node 22 for the frontend build only. Nothing runs on it at runtime.
if ! command -v node >/dev/null || [ "$(node -v | cut -d. -f1 | tr -d v)" -lt 20 ]; then
    note "installing Node.js 22 (build-time only)"
    mkdir -p /etc/apt/keyrings
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list
    apt-get update -qq && apt-get install -y -qq nodejs >/dev/null
fi
note "node: $(node -v)"

if [ "$WITH_DOCKER" -eq 1 ] && ! command -v docker >/dev/null; then
    note "installing docker (for building the installer ramdisk)"
    apt-get install -y -qq docker.io >/dev/null
    systemctl enable --now docker >/dev/null
fi

# ---------------------------------------------------------------------------
# User and files
# ---------------------------------------------------------------------------

say "installing to $INSTALL_DIR"
id doz >/dev/null 2>&1 || useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin doz
mkdir -p "$INSTALL_DIR" /etc/doz /etc/doz/cimc "$INSTALL_DIR/installer/assets" "$INSTALL_DIR/installer/tftp"

if [ "$(readlink -f "$SRC_DIR")" != "$(readlink -f "$INSTALL_DIR")" ]; then
    rsync -a --delete \
        --exclude '.venv' --exclude 'node_modules' --exclude '.run' \
        --exclude 'frontend/dist' --exclude 'installer/assets' --exclude '.env' \
        "$SRC_DIR/" "$INSTALL_DIR/"
fi
chown -R doz:doz "$INSTALL_DIR"
chown root:doz /etc/doz; chmod 750 /etc/doz
chown doz:doz /etc/doz/cimc; chmod 700 /etc/doz/cimc

# ---------------------------------------------------------------------------
# PostgreSQL and Redis
# ---------------------------------------------------------------------------

say "database"
systemctl enable --now postgresql redis-server >/dev/null
if [ -f /etc/doz/doz.env ] && grep -q '^DOZ_DATABASE_URL=' /etc/doz/doz.env; then
    DB_PASS="$(sed -n 's|^DOZ_DATABASE_URL=postgresql+psycopg://doz:\([^@]*\)@.*|\1|p' /etc/doz/doz.env)"
    note "keeping existing database credentials"
else
    DB_PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
fi
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='doz'" | grep -q 1; then
    sudo -u postgres psql -qc "CREATE USER doz WITH PASSWORD '$DB_PASS';"
else
    sudo -u postgres psql -qc "ALTER USER doz WITH PASSWORD '$DB_PASS';"
fi
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='doz'" | grep -q 1 \
    || sudo -u postgres createdb -O doz doz
note "postgres role and database ready"

# ---------------------------------------------------------------------------
# Environment file
# ---------------------------------------------------------------------------

say "configuration"
if [ "$PROXY_DHCP" -eq 1 ]; then PIN=false; else PIN=true; fi
if [ ! -f /etc/doz/doz.env ]; then
    JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    cat > /etc/doz/doz.env <<ENV
# DedicatedOZ production configuration. Read by the systemd units.
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

DOZ_REDFISH_VERIFY_TLS=false
DOZ_REDFISH_TIMEOUT_SECONDS=30
DOZ_REDFISH_MAX_RETRIES=3

DOZ_INSTALL_TIMEOUT_SECONDS=2700
DOZ_CALLBACK_TOKEN_TTL_SECONDS=14400
DOZ_REQUIRE_WIPE_BEFORE_STOCK=true
# ${PIN}: authoritative dnsmasq keys leases on MAC (safe to pin); proxy mode
# cannot guarantee the installer gets the same address as iPXE did.
DOZ_BOOT_PIN_CLIENT_IP=${PIN}

# nginx is in front of the API and sets X-Forwarded-For.
DOZ_TRUST_PROXY_HEADERS=true
DOZ_CORS_ORIGINS=http://${MGMT_IP}

DOZ_INSTALLER_TEMPLATE_DIR=${INSTALL_DIR}/installer/templates
DOZ_IPMITOOL_PATH=/usr/bin/ipmitool
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
sudo -u doz bash -c "
    set -e
    cd '$INSTALL_DIR/backend'
    [ -x .venv/bin/python ] || $PYTHON -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -e .
"
note "python dependencies installed"

INIT_OUT="$(sudo -u doz bash -c "set -a; . /etc/doz/doz.env; set +a; cd '$INSTALL_DIR/backend' && .venv/bin/python -m scripts.init_db --admin-email '$ADMIN_EMAIL'")"
echo "$INIT_OUT" | sed 's/^/    /'
ADMIN_PASSWORD="$(echo "$INIT_OUT" | sed -n 's/^admin password: *//p')"

# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

say "frontend"
sudo -u doz bash -c "cd '$INSTALL_DIR/frontend' && npm ci --silent && npm run build --silent" >/dev/null
note "built to $INSTALL_DIR/frontend/dist"

# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------

say "services"
unit() {
    # unit <name> <description> <exec>
    cat > "/etc/systemd/system/$1.service" <<UNIT
[Unit]
Description=DedicatedOZ $2
After=network-online.target postgresql.service redis-server.service
Wants=network-online.target

[Service]
Type=simple
User=doz
Group=doz
WorkingDirectory=$INSTALL_DIR/backend
EnvironmentFile=/etc/doz/doz.env
ExecStart=$3
Restart=always
RestartSec=3
# The worker holds BMC credentials in memory; keep the process locked down.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/etc/doz/cimc $INSTALL_DIR/installer/assets

[Install]
WantedBy=multi-user.target
UNIT
}
unit doz-api    "API"            "$INSTALL_DIR/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1"
unit doz-worker "job worker"     "$INSTALL_DIR/backend/.venv/bin/celery -A app.workers.celery_app.celery_app worker --loglevel=info -Q provision,power,poll --concurrency=4"
unit doz-beat   "scheduler"      "$INSTALL_DIR/backend/.venv/bin/celery -A app.workers.celery_app.celery_app beat --loglevel=info"
systemctl daemon-reload
systemctl enable --now doz-api doz-worker doz-beat >/dev/null
note "doz-api, doz-worker, doz-beat enabled"

# ---------------------------------------------------------------------------
# nginx
# ---------------------------------------------------------------------------

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

    # API, netboot rail, docs. Websocket headers are for the SOL console.
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

# Boot assets: kernels, initrds, ISOs. Plain HTTP, no redirects -- the CIMC's
# virtual media cannot follow one and cannot validate our certificate.
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
systemctl enable --now nginx >/dev/null
systemctl reload nginx
note "portal on http://$MGMT_IP, boot assets on http://$MGMT_IP:8080"

# ---------------------------------------------------------------------------
# iPXE + dnsmasq
# ---------------------------------------------------------------------------

say "PXE"
TFTP="$INSTALL_DIR/installer/tftp"
for f in undionly.kpxe ipxe.efi; do
    if [ ! -s "$TFTP/$f" ]; then
        note "fetching $f from boot.ipxe.org"
        curl -fsSL --retry 3 -o "$TFTP/$f" "https://boot.ipxe.org/$f"
    fi
done
chown -R doz:doz "$TFTP"

# systemd-resolved owns 127.0.0.53:53; we do not want DNS from dnsmasq at all.
cat > /etc/dnsmasq.d/doz.conf <<DNSMASQ
# DedicatedOZ PXE. Generated by deploy/install-management-server.sh.
port=0
interface=$IFACE
bind-interfaces
log-dhcp

enable-tftp
tftp-root=$TFTP

# The NIC's PXE ROM gets iPXE over TFTP; iPXE (user class "iPXE", option 175)
# gets the control plane URL and never touches TFTP again.
dhcp-match=set:ipxe,175
dhcp-match=set:efi64,option:client-arch,7
dhcp-match=set:efi64,option:client-arch,9
DNSMASQ

if [ -n "$DHCP_RANGE" ]; then
    cat >> /etc/dnsmasq.d/doz.conf <<DNSMASQ

# Authoritative DHCP for the flat network.
dhcp-range=${DHCP_RANGE},${NET_MASK},12h
dhcp-authoritative
${GATEWAY:+dhcp-option=option:router,$GATEWAY}
dhcp-option=option:dns-server,1.1.1.1,8.8.8.8
# Key leases on MAC only. iPXE, the ramdisk and the OS installer each send a
# different client-id; without this the same machine can get three addresses
# during one install, and the platform pins boot files to the first one.
dhcp-ignore-clid

dhcp-boot=tag:!ipxe,tag:!efi64,undionly.kpxe,,${MGMT_IP}
dhcp-boot=tag:!ipxe,tag:efi64,ipxe.efi,,${MGMT_IP}
dhcp-boot=tag:ipxe,http://${MGMT_IP}/boot/ipxe
DNSMASQ
else
    cat >> /etc/dnsmasq.d/doz.conf <<DNSMASQ

# Proxy DHCP: the existing DHCP server keeps handing out addresses; this only
# adds PXE boot information alongside its replies.
dhcp-range=${NET_ADDR},proxy,${NET_MASK}
pxe-prompt="DedicatedOZ",1
pxe-service=tag:!ipxe,x86PC,"Boot DedicatedOZ (BIOS)",undionly.kpxe
pxe-service=tag:!ipxe,X86-64_EFI,"Boot DedicatedOZ (UEFI)",ipxe.efi
pxe-service=tag:ipxe,x86PC,"DedicatedOZ",http://${MGMT_IP}/boot/ipxe
pxe-service=tag:ipxe,X86-64_EFI,"DedicatedOZ",http://${MGMT_IP}/boot/ipxe
DNSMASQ
fi

dnsmasq --test -C /etc/dnsmasq.conf >/dev/null 2>&1 || { dnsmasq --test; die "dnsmasq config failed validation"; }
systemctl enable --now dnsmasq >/dev/null
systemctl restart dnsmasq
note "dnsmasq serving TFTP from $TFTP"

# ---------------------------------------------------------------------------
# Firewall
# ---------------------------------------------------------------------------

if ufw status 2>/dev/null | grep -q "Status: active"; then
    say "firewall"
    ufw allow 22/tcp  >/dev/null
    ufw allow 80/tcp  >/dev/null
    ufw allow 8080/tcp >/dev/null
    ufw allow 67/udp  >/dev/null
    ufw allow 69/udp  >/dev/null
    ufw allow 4011/udp >/dev/null
    note "opened 22, 80, 8080/tcp and 67, 69, 4011/udp"
fi

# ---------------------------------------------------------------------------
# Optional: images and ramdisk
# ---------------------------------------------------------------------------

if [ "$FETCH_IMAGES" -eq 1 ]; then
    say "OS installer images (this downloads ~2.5 GB)"
    sudo -u doz "$INSTALL_DIR/deploy/fetch-os-images.sh" --assets "$INSTALL_DIR/installer/assets"
fi

if [ "$WITH_DOCKER" -eq 1 ]; then
    say "installer ramdisk"
    "$INSTALL_DIR/installer/build-ramdisk.sh" --out "$INSTALL_DIR/installer/assets/doz-installer" || \
        note "ramdisk build failed; see installer/README.md"
    chown -R doz:doz "$INSTALL_DIR/installer/assets"
fi

# ---------------------------------------------------------------------------

say "checking"
sleep 2
if curl -fsS "http://127.0.0.1/health" | grep -q '"status":"ok"'; then
    note "API healthy through nginx"
else
    note "API not answering yet: journalctl -u doz-api"
fi

cat <<SUMMARY

=======================================================================
 DedicatedOZ is installed.
=======================================================================

 Portal:        http://${MGMT_IP}
 API docs:      http://${MGMT_IP}/docs
 Boot assets:   http://${MGMT_IP}:8080
 Admin login:   ${ADMIN_EMAIL}
SUMMARY
if [ -n "$ADMIN_PASSWORD" ]; then
    echo " Password:      ${ADMIN_PASSWORD}"
    echo "                (shown once; reset with: ./doz.sh reset-admin ${ADMIN_EMAIL})"
else
    echo " Password:      unchanged (account already existed)"
fi
cat <<SUMMARY

 Config:        /etc/doz/doz.env
 CIMC secrets:  /etc/doz/cimc/
 Logs:          journalctl -f -u doz-api -u doz-worker -u doz-beat
 Manage:        cd ${INSTALL_DIR} && ./doz.sh status|restart|logs

 Still to do before the first install:
   1. ./deploy/fetch-os-images.sh        (or re-run with --fetch-images)
   2. ./doz.sh ramdisk                    (needs docker; or --with-docker)
   3. Point the servers' CIMCs and NICs at this network, then follow
      docs/GETTING-STARTED.md
SUMMARY
