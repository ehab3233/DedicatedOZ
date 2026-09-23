#!/usr/bin/env bash
# Check an installed management server end to end, against the BMC simulator.
#
#   sudo ./deploy/smoke-test.sh
#
# Starts the simulator (SIM-0001), gives the admin account a fresh password,
# then drives power and the serial console through nginx exactly as the panel
# does. Needs the simulator: sudo apt install --no-install-recommends openipmi.
# Changes the admin password -- run it on a fresh install, or reset it after.

set -euo pipefail
ROOT=/opt/doz
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
[ -f /etc/systemd/system/doz.service ] || { echo "doz is not installed" >&2; exit 1; }

. /etc/doz/install.conf
EMAIL="${ADMIN_EMAIL:-admin@example.com}"

echo "== service =="
failed=0
for unit in doz doz-api doz-worker-power doz-worker-provision doz-worker-poll doz-beat doz-pxe nginx postgresql redis-server; do
    [ "$unit" = doz-pxe ] && [ ! -f /etc/systemd/system/doz-pxe.service ] && continue
    state="$(systemctl is-active "$unit" 2>/dev/null || true)"
    printf '  %-44s %s\n' "$unit" "$state"
    [ "$state" = active ] || failed=1
done
[ "$failed" -eq 0 ] || { echo "not every unit is active: journalctl -u 'doz*'" >&2; exit 1; }
[ "$(systemctl is-enabled doz)" = enabled ] || { echo "doz.service is not enabled at boot" >&2; exit 1; }
echo "  doz.service enabled at boot                  yes"

echo "== simulator =="
"$ROOT/doz.sh" sim start | sed 's/^/  /'

PASSWORD="$("$ROOT/doz.sh" reset-admin "$EMAIL" | sed -n 's/^new password: //p')"
[ -n "$PASSWORD" ] || { echo "could not reset the admin password" >&2; exit 1; }

runuser -u doz -- bash -c "cd $ROOT/backend && .venv/bin/python -m scripts.smoke_test --email '$EMAIL' --password '$PASSWORD'"
