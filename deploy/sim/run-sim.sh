#!/usr/bin/env bash
# A pretend C220 BMC on this machine, for trying the platform without hardware.
#
# Runs OpenIPMI's ipmi_sim -- a real IPMI-over-LAN (RMCP+) implementation --
# with a chassis hook that remembers power state and boot device, and a fake
# serial port that prints a POST screen and a Linux login on power-on/reset.
# ipmitool, and so the whole platform, cannot tell it from a BMC for the
# things that matter here: power on/off/reset/cycle, boot device, and the
# Serial-over-LAN console.
#
#   run-sim.sh start [--user U] [--password P] [--port 9623] [--serial-port 9603] [--state DIR]
#   run-sim.sh stop | status
#
# Needs: ipmi_sim (apt install openipmi), ipmitool, python3.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${DOZ_SIM_STATE:-$HERE/../../.run/sim}"
ADDR=127.0.0.1
IPMI_PORT=9623
SERIAL_PORT=9603
USER_NAME=admin
PASSWORD=sim-password

cmd="${1:-status}"; shift || true
while [ $# -gt 0 ]; do
    case "$1" in
        --user)     USER_NAME="$2"; shift 2 ;;
        --password) PASSWORD="$2"; shift 2 ;;
        --port)     IPMI_PORT="$2"; shift 2 ;;
        --serial-port) SERIAL_PORT="$2"; shift 2 ;;
        --addr)     ADDR="$2"; shift 2 ;;
        --state)    STATE="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
mkdir -p "$STATE"
STATE="$(cd "$STATE" && pwd)"

running() { [ -f "$STATE/$1.pid" ] && kill -0 "$(cat "$STATE/$1.pid")" 2>/dev/null; }

stop() {
    for name in ipmi_sim serial; do
        if running "$name"; then
            kill "$(cat "$STATE/$name.pid")" 2>/dev/null || true
            echo "stopped $name"
        fi
        rm -f "$STATE/$name.pid"
    done
}

case "$cmd" in
    start)
        command -v ipmi_sim >/dev/null || { echo "ipmi_sim not found: sudo apt install openipmi" >&2; exit 1; }
        command -v ipmitool >/dev/null || { echo "ipmitool not found: sudo apt install ipmitool" >&2; exit 1; }
        stop >/dev/null
        [ -f "$STATE/power" ] || echo 1 > "$STATE/power"
        sed -e "s|@ADDR@|$ADDR|; s|@IPMI_PORT@|$IPMI_PORT|; s|@SERIAL_PORT@|$SERIAL_PORT|" \
            -e "s|@HERE@|$HERE|; s|@USER@|$USER_NAME|; s|@PASSWORD@|$PASSWORD|" \
            "$HERE/lan.conf.in" > "$STATE/lan.conf"
        chmod 600 "$STATE/lan.conf"

        SIM_STATE="$STATE" setsid python3 "$HERE/fake_serial.py" "$SERIAL_PORT" \
            > "$STATE/serial.log" 2>&1 < /dev/null &
        echo $! > "$STATE/serial.pid"
        sleep 0.5
        # A serial port that failed to bind would leave ipmi_sim's SOL talking
        # to whatever else is on that port -- a silently wrong simulator.
        if ! running serial; then
            echo "fake serial port could not start on $SERIAL_PORT:" >&2
            tail -3 "$STATE/serial.log" >&2
            exit 1
        fi

        SIM_STATE="$STATE" setsid ipmi_sim -c "$STATE/lan.conf" -f "$HERE/sim.emu" -s "$STATE" -n \
            > "$STATE/ipmi_sim.log" 2>&1 < /dev/null &
        echo $! > "$STATE/ipmi_sim.pid"

        for _ in $(seq 1 20); do
            if IPMI_PASSWORD="$PASSWORD" ipmitool -I lanplus -H "$ADDR" -p "$IPMI_PORT" \
                   -U "$USER_NAME" -E -C 3 chassis power status >/dev/null 2>&1; then
                echo "BMC simulator up: IPMI on $ADDR:$IPMI_PORT, user $USER_NAME"
                echo "state in $STATE"
                exit 0
            fi
            sleep 0.5
        done
        echo "simulator did not answer; see $STATE/ipmi_sim.log" >&2
        exit 1
        ;;
    stop)
        stop ;;
    status)
        for name in ipmi_sim serial; do
            if running "$name"; then echo "$name: running"; else echo "$name: stopped"; fi
        done
        [ -f "$STATE/power" ] && echo "power: $( [ "$(cat "$STATE/power")" = 1 ] && echo on || echo off)"
        [ -f "$STATE/boot" ] && echo "boot device: $(cat "$STATE/boot")"
        ;;
    *)
        echo "usage: run-sim.sh start|stop|status" >&2; exit 2 ;;
esac
