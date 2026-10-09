#!/usr/bin/env bash
# DedicatedOZ run script.
#
# One entry point for the three ways this runs:
#
#   systemd  the management server: the `doz` service, installed by
#            `sudo ./doz.sh install ...` (deploy/install-management-server.sh)
#   dev      local processes against a local Postgres/Redis, logs in .run/
#   compose  docker compose, everything in containers
#
# The mode is detected: an installed doz.service means systemd, a running
# docker compose project means compose, otherwise dev. Override with
# DOZ_MODE=dev|compose|systemd.
#
#   sudo ./doz.sh install --ip IP (--dhcp-range A,B | --proxy-dhcp | --external-dhcp | --no-pxe)
#                          install as the `doz` Ubuntu service (see deploy/)
#   sudo ./doz.sh update   re-run the installer with the saved settings
#   ./doz.sh up            start everything (systemd: systemctl start doz)
#   ./doz.sh down          stop everything
#   ./doz.sh restart       stop, start
#   ./doz.sh status        every part, plus which job queues have a worker
#   ./doz.sh logs [part]   follow logs (api | power | provision | poll | beat | pxe | all)
#   ./doz.sh sim start|stop|status|events|remove
#                          a simulated BMC on this machine, registered as SIM-0001,
#                          for trying power, sensors, the event log and the serial
#                          console without hardware (events: add sample log entries)
#   ./doz.sh init          create/upgrade the schema, seed templates, create the admin
#   ./doz.sh reset-admin EMAIL    set a new password on an admin account
#   ./doz.sh test          run the backend tests
#   ./doz.sh lint          ruff + tsc
#   ./doz.sh shell         python shell with the app importable
#   ./doz.sh bench HOST    run the five hardware validation tests against a CIMC
#   ./doz.sh assets        fetch OS installer images into installer/assets
#   ./doz.sh ramdisk       build the installer ramdisk (installs docker and cpio if missing)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
RUN_DIR="$ROOT/.run"
VENV="$BACKEND/.venv"
PY="$VENV/bin/python"
CELERY_APP="app.workers.celery_app.celery_app"

# ---------------------------------------------------------------------------

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

detect_mode() {
    if [ -n "${DOZ_MODE:-}" ]; then echo "$DOZ_MODE"; return; fi
    if [ -f /etc/systemd/system/doz.service ]; then
        echo systemd; return
    fi
    if command -v docker >/dev/null 2>&1 \
       && docker compose -f "$ROOT/docker-compose.yml" ps --status running -q 2>/dev/null | grep -q .; then
        echo compose; return
    fi
    echo dev
}

load_env() {
    # .env at the repo root is the dev config. Exported so child processes see it.
    if [ -f "$ROOT/.env" ]; then
        set -a; . "$ROOT/.env"; set +a
    fi
    export DOZ_INSTALLER_TEMPLATE_DIR="${DOZ_INSTALLER_TEMPLATE_DIR:-$ROOT/installer/templates}"
}

ensure_venv() {
    if [ ! -x "$PY" ]; then
        say "creating virtualenv"
        python3 -m venv "$VENV"
        "$VENV/bin/pip" install -q --upgrade pip
        "$VENV/bin/pip" install -q -e "$BACKEND[dev]"
    fi
}

ensure_frontend_deps() {
    if [ ! -d "$FRONTEND/node_modules" ]; then
        say "installing frontend dependencies"
        (cd "$FRONTEND" && npm install --silent)
    fi
}

ensure_env_file() {
    if [ ! -f "$ROOT/.env" ]; then
        say "no .env found; creating one from .env.example with a fresh secret"
        cp "$ROOT/.env.example" "$ROOT/.env"
        secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
        sed -i "s|^DOZ_JWT_SECRET=.*|DOZ_JWT_SECRET=$secret|" "$ROOT/.env"
        warn "edit .env: DOZ_CONTROL_PLANE_URL, DOZ_BOOT_ASSET_BASE_URL and the CIMC credentials"
    fi
}

wait_for() {
    # wait_for <label> <command...>
    label="$1"; shift
    for _ in $(seq 1 30); do
        if "$@" >/dev/null 2>&1; then return 0; fi
        sleep 1
    done
    die "$label did not become ready"
}

# ---------------------------------------------------------------------------
# dev mode
# ---------------------------------------------------------------------------

dev_check_services() {
    load_env
    db_url="${DOZ_DATABASE_URL:-postgresql+psycopg://doz:doz@localhost:5432/doz}"
    redis_url="${DOZ_REDIS_URL:-redis://localhost:6379/0}"

    if ! "$PY" - "$db_url" <<'PY' >/dev/null 2>&1
import sys, sqlalchemy
sqlalchemy.create_engine(sys.argv[1]).connect().close()
PY
    then
        die "cannot reach PostgreSQL at $db_url
  Start one with:  docker compose up -d postgres
  or locally:      sudo service postgresql start && sudo -u postgres psql -c \"CREATE USER doz WITH PASSWORD 'doz' SUPERUSER\" && sudo -u postgres createdb -O doz doz"
    fi
    if ! "$PY" - "$redis_url" <<'PY' >/dev/null 2>&1
import sys, redis
redis.Redis.from_url(sys.argv[1]).ping()
PY
    then
        die "cannot reach Redis at $redis_url
  Start one with:  docker compose up -d redis
  or locally:      redis-server --daemonize yes"
    fi
}

dev_start_one() {
    # dev_start_one <name> <workdir> <command...>
    name="$1"; workdir="$2"; shift 2
    pidfile="$RUN_DIR/$name.pid"
    logfile="$RUN_DIR/$name.log"
    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "  $name already running (pid $(cat "$pidfile"))"
        return
    fi
    # exec so the pid is the service itself, not a wrapper shell; setsid so
    # it gets its own process group (uvicorn --reload and vite both spawn
    # children, and `down` kills the whole group). All three stdio fds are
    # detached from the caller, or `./doz.sh up | tee` would never return.
    (
        cd "$workdir" || exit 1
        exec setsid nohup "$@" >"$logfile" 2>&1 </dev/null
    ) &
    echo $! >"$pidfile"
    echo "  $name started (pid $(cat "$pidfile")) -> $logfile"
}

dev_up() {
    ensure_env_file
    ensure_venv
    ensure_frontend_deps
    dev_check_services
    mkdir -p "$RUN_DIR"
    load_env

    say "initialising database"
    (cd "$BACKEND" && "$PY" -m scripts.init_db --admin-email "${DOZ_ADMIN_EMAIL:-admin@example.com}" \
        ${DOZ_ADMIN_PASSWORD:+--admin-password "$DOZ_ADMIN_PASSWORD"})

    say "starting services"
    dev_start_one api      "$BACKEND"  "$VENV/bin/uvicorn" app.main:app --host 0.0.0.0 --port 8000 --reload
    # Power and polling on one worker, provisioning on another: a reinstall
    # holds its worker slot for twenty minutes, and must not hold the power button.
    dev_start_one worker   "$BACKEND"  "$VENV/bin/celery" -A "$CELERY_APP" worker -n power@%h --loglevel=info -Q power,poll --concurrency=4
    dev_start_one provision "$BACKEND" "$VENV/bin/celery" -A "$CELERY_APP" worker -n provision@%h --loglevel=info -Q provision --concurrency=4
    dev_start_one beat     "$BACKEND"  "$VENV/bin/celery" -A "$CELERY_APP" beat --loglevel=info
    dev_start_one frontend "$FRONTEND" npm run dev -- --host 0.0.0.0

    wait_for "API" curl -fsS http://localhost:8000/health
    echo
    say "up"
    echo "  portal   http://localhost:5173"
    echo "  api docs http://localhost:8000/docs"
    echo "  logs     ./doz.sh logs"
}

dev_down() {
    [ -d "$RUN_DIR" ] || { echo "nothing running"; return; }
    for pidfile in "$RUN_DIR"/*.pid; do
        [ -f "$pidfile" ] || continue
        name="$(basename "$pidfile" .pid)"
        pid="$(cat "$pidfile")"
        if kill -0 "$pid" 2>/dev/null; then
            # The service is a session leader (setsid), so a negative pid
            # takes its whole process group with it.
            kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
            for _ in 1 2 3 4 5; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 1
            done
            kill -0 "$pid" 2>/dev/null && kill -KILL -- "-$pid" 2>/dev/null
            echo "  stopped $name"
        fi
        rm -f "$pidfile"
    done
}

dev_status() {
    [ -d "$RUN_DIR" ] || { echo "nothing running"; return; }
    for name in api worker provision beat frontend; do
        pidfile="$RUN_DIR/$name.pid"
        if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
            printf '  %-10s running (pid %s)\n' "$name" "$(cat "$pidfile")"
        else
            printf '  %-10s stopped\n' "$name"
        fi
    done
    curl -fsS http://localhost:8000/health 2>/dev/null && echo || echo "  api health: unreachable"
}

dev_logs() {
    svc="${1:-all}"
    mkdir -p "$RUN_DIR"
    if [ "$svc" = all ]; then
        tail -n 50 -F "$RUN_DIR"/*.log
    else
        tail -n 100 -F "$RUN_DIR/$svc.log"
    fi
}

# ---------------------------------------------------------------------------
# compose mode
# ---------------------------------------------------------------------------

compose() { docker compose -f "$ROOT/docker-compose.yml" "$@"; }

compose_up() {
    ensure_env_file
    say "building and starting containers"
    compose up -d --build
    wait_for "API" curl -fsS http://localhost:8000/health
    say "initialising database"
    compose exec api python -m scripts.init_db --admin-email "${DOZ_ADMIN_EMAIL:-admin@example.com}"
    echo
    say "up"
    echo "  api docs http://localhost:8000/docs"
    echo "  the portal is served by the API container only in a systemd install;"
    echo "  for compose, run the frontend with: cd frontend && npm run dev"
}

# ---------------------------------------------------------------------------
# systemd mode
# ---------------------------------------------------------------------------

SYSTEMD_PARTS="doz-api doz-worker-power doz-worker-provision doz-worker-poll doz-beat doz-pxe"

need_root() { [ "$(id -u)" -eq 0 ] || die "run with sudo"; }

sd_up()      { sudo systemctl start doz; sd_status; }
sd_down()    { sudo systemctl stop doz; }
sd_restart() { sudo systemctl restart doz; sleep 2; sd_status; }
sd_status() {
    printf '  %-22s %s\n' "doz (umbrella)" "$(systemctl is-active doz 2>/dev/null)"
    for part in $SYSTEMD_PARTS; do
        [ -f "/etc/systemd/system/$part.service" ] || continue
        printf '  %-22s %s\n' "$part" "$(systemctl is-active "$part" 2>/dev/null)"
    done
    redis_unit=redis-server
    [ -f /etc/doz/install.conf ] && redis_unit="$(sed -n 's/^REDIS_UNIT=//p' /etc/doz/install.conf)"
    for dep in postgresql "${redis_unit:-redis-server}" nginx; do
        printf '  %-22s %s\n' "$dep" "$(systemctl is-active "$dep" 2>/dev/null)"
    done
    printf '  %-22s ' "api health"
    curl -fsS http://127.0.0.1/health 2>/dev/null && echo || echo "unreachable"
    printf '  %-22s ' "workers answering"
    sd_celery inspect ping --timeout 3 2>/dev/null | grep -c pong || true
}
sd_logs() {
    part="${1:-all}"
    case "$part" in
        all)       sudo journalctl -f -u 'doz*' ;;
        api|beat|pxe) sudo journalctl -f -u "doz-$part" ;;
        power|provision|poll) sudo journalctl -f -u "doz-worker-$part" ;;
        *)         sudo journalctl -f -u "$part" ;;
    esac
}
as_doz_user() {
    if [ "$(id -u)" -eq 0 ]; then runuser -u doz -- "$@"; else sudo -u doz "$@"; fi
}
sd_celery() {
    as_doz_user bash -c 'set -a; . /etc/doz/doz.env; set +a; cd /opt/doz/backend && exec .venv/bin/celery -A app.workers.celery_app.celery_app "$@"' _ "$@"
}
sd_python() {
    # Run python as the doz user with the production environment. Arguments
    # are passed through; a script may also be fed on stdin with `-`.
    as_doz_user bash -c 'set -a; . /etc/doz/doz.env; set +a; cd /opt/doz/backend && exec .venv/bin/python "$@"' _ "$@"
}

# ---------------------------------------------------------------------------
# shared commands
# ---------------------------------------------------------------------------

cmd_init() {
    case "$MODE" in
        systemd) sd_python -m scripts.init_db "$@" ;;
        compose) compose exec api python -m scripts.init_db "$@" ;;
        dev)     ensure_venv; load_env; (cd "$BACKEND" && "$PY" -m scripts.init_db "$@") ;;
    esac
}

cmd_reset_admin() {
    email="${1:-}"; [ -n "$email" ] || die "usage: ./doz.sh reset-admin EMAIL"
    password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
    script="
from app.db import SessionLocal
from app.models import Customer
from app.security import hash_password
from sqlalchemy import select
with SessionLocal() as db:
    c = db.execute(select(Customer).where(Customer.email == '$email'.lower())).scalar_one_or_none()
    if c is None:
        raise SystemExit('no account with that email')
    c.password_hash = hash_password('$password'); c.is_active = True; db.add(c); db.commit()
print('password reset for $email')
"
    case "$MODE" in
        systemd) printf '%s' "$script" | sd_python - ;;
        compose) printf '%s' "$script" | compose exec -T api python - ;;
        dev)     ensure_venv; load_env; (cd "$BACKEND" && printf '%s' "$script" | "$PY" -) ;;
    esac
    echo "new password: $password"
}

cmd_test() {
    ensure_venv
    (cd "$BACKEND" && "$PY" -m pytest -q "$@")
}

cmd_lint() {
    ensure_venv; ensure_frontend_deps
    (cd "$BACKEND" && "$VENV/bin/ruff" check app scripts tests)
    (cd "$FRONTEND" && npx tsc --noEmit)
    say "lint clean"
}

cmd_shell() {
    ensure_venv; load_env
    (cd "$BACKEND" && "$PY" -i -c "
from app.db import SessionLocal
from app.models import *
from sqlalchemy import select
db = SessionLocal()
print('db session ready: db, select, and all models are in scope')
")
}

cmd_bench() {
    host="${1:-}"; [ -n "$host" ] || die "usage: ./doz.sh bench CIMC_IP [bench_validate args...]"
    shift
    ensure_venv
    (cd "$BACKEND" && "$PY" -m scripts.bench_validate --host "$host" "$@")
}

cmd_sim() {
    action="${1:-status}"; shift || true
    case "$MODE" in
        systemd)
            need_root
            state=/var/lib/doz/sim
            user_name=admin
            password="$(cat "$state/.password" 2>/dev/null || python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
            # The installed copy: the doz user cannot read a checkout in
            # someone's home directory (750 on current Ubuntu).
            run() { runuser -u doz -- env DOZ_SIM_STATE="$state" /opt/doz/deploy/sim/run-sim.sh "$@"; }
            register() { sd_python -m scripts.sim_server --user "$user_name" --password "$password" "$@"; }
            ;;
        *)
            ensure_venv; load_env
            state="$ROOT/.run/sim"
            # The env secrets backend cannot be written to, so the simulator
            # takes the fleet default credential instead.
            user_name="${DOZ_CIMC_DEFAULT_USER:-admin}"
            password="${DOZ_CIMC_DEFAULT_PASS:-sim-password}"
            run() { DOZ_SIM_STATE="$state" "$ROOT/deploy/sim/run-sim.sh" "$@"; }
            register() { (cd "$BACKEND" && "$PY" -m scripts.sim_server --user "$user_name" --password "$password" "$@"); }
            ;;
    esac
    case "$action" in
        start)
            command -v ipmi_sim >/dev/null || die "ipmi_sim is not installed: sudo apt install --no-install-recommends openipmi"
            # The openipmi package also installs openipmi.service, which loads
            # drivers for *this machine's own* BMC. A VM has none, so it fails
            # on every boot and marks the system degraded. The simulator does
            # not use it.
            if [ "$(id -u)" -eq 0 ] && systemctl is-enabled --quiet openipmi.service 2>/dev/null; then
                systemctl disable --now openipmi.service >/dev/null 2>&1 || true
                systemctl reset-failed openipmi.service >/dev/null 2>&1 || true
                echo "disabled openipmi.service (local BMC drivers; not needed by the simulator)"
            fi
            mkdir -p "$state"
            [ "$MODE" = systemd ] && { chown doz:doz "$state"; printf '%s' "$password" > "$state/.password"; chown doz:doz "$state/.password"; chmod 600 "$state/.password"; }
            run start --user "$user_name" --password "$password"
            register
            echo
            echo "Open the panel: Manage -> Fleet -> SIM-0001. Power buttons and the serial"
            echo "console drive the simulator exactly as they would a CIMC over IPMI."
            ;;
        stop)   run stop ;;
        status) run status ;;
        events) run events ;;
        remove) run stop; register --remove ;;
        *) die "usage: ./doz.sh sim start|stop|status|events|remove" ;;
    esac
}

cmd_install() {
    need_root
    exec "$ROOT/deploy/install-management-server.sh" --src "$ROOT" "$@"
}

cmd_update() {
    need_root
    [ -f /etc/doz/install.conf ] || die "not installed yet: use ./doz.sh install first"
    exec "$ROOT/deploy/install-management-server.sh" --src "$ROOT" "$@"
}

cmd_assets()  { exec "$ROOT/deploy/fetch-os-images.sh" "$@"; }
cmd_ramdisk() {
    # docker assembles the Alpine rootfs, cpio packs it. Installed here rather
    # than refused: this is the one command a fresh management server runs
    # before its first reinstall.
    local -a missing=()
    command -v docker >/dev/null 2>&1 || missing+=(docker.io)
    command -v cpio >/dev/null 2>&1 || missing+=(cpio)
    if [ "${#missing[@]}" -gt 0 ]; then
        [ "$(id -u)" -eq 0 ] || die "the ramdisk build needs ${missing[*]}: sudo apt install -y ${missing[*]}"
        say "installing ${missing[*]}"
        apt-get install -y -qq "${missing[@]}" >/dev/null \
            || { apt-get update -qq; apt-get install -y -qq "${missing[@]}" >/dev/null; }
        command -v docker >/dev/null 2>&1 && systemctl enable --now docker >/dev/null 2>&1 || true
    fi
    bash "$ROOT/installer/build-ramdisk.sh" --out "$ROOT/installer/assets/doz-installer" "$@"
    # On an installed system the asset tree belongs to the service user.
    if [ "$(id -u)" -eq 0 ] && id doz >/dev/null 2>&1 && [ -f /etc/doz/install.conf ]; then
        chown -R doz:doz "$ROOT/installer/assets/doz-installer"
    fi
}

# ---------------------------------------------------------------------------

MODE="$(detect_mode)"
cmd="${1:-help}"; shift || true

case "$cmd" in
    up)
        case "$MODE" in dev) dev_up ;; compose) compose_up ;; systemd) sd_up ;; esac ;;
    down|stop)
        case "$MODE" in dev) dev_down ;; compose) compose down ;; systemd) sd_down ;; esac ;;
    restart)
        case "$MODE" in dev) dev_down; dev_up ;; compose) compose restart ;; systemd) sd_restart ;; esac ;;
    status)
        echo "mode: $MODE"
        case "$MODE" in dev) dev_status ;; compose) compose ps ;; systemd) sd_status ;; esac ;;
    logs)
        case "$MODE" in dev) dev_logs "$@" ;; compose) compose logs -f "$@" ;; systemd) sd_logs "$@" ;; esac ;;
    install)      cmd_install "$@" ;;
    update)       cmd_update "$@" ;;
    sim)          cmd_sim "$@" ;;
    init)         cmd_init "$@" ;;
    reset-admin)  cmd_reset_admin "$@" ;;
    test)         cmd_test "$@" ;;
    lint)         cmd_lint ;;
    shell)        cmd_shell ;;
    bench)        cmd_bench "$@" ;;
    assets)       cmd_assets "$@" ;;
    ramdisk)      cmd_ramdisk "$@" ;;
    help|-h|--help)
        sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//' ;;
    *)
        die "unknown command: $cmd (try ./doz.sh help)" ;;
esac
