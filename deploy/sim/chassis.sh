#!/bin/sh
# ipmi_sim chassis_control hook: <device> get|set|check [parm [val]]...
state=${SIM_STATE:-/tmp/doz-sim}
mkdir -p "$state"
[ -f "$state/power" ] || echo 0 > "$state/power"
[ -f "$state/boot" ] || echo default > "$state/boot"
echo "$(date +%T) $*" >> "$state/chassis.log"
dev=$1; op=$2; shift 2
case "$op" in
  get)
    for p in "$@"; do
      case "$p" in
        power) echo "power:$(cat "$state/power")" ;;
        boot)  echo "boot:$(cat "$state/boot")" ;;
        *)     echo "$p:0" ;;
      esac
    done ;;
  check) exit 0 ;;
  set)
    while [ $# -gt 0 ]; do
      p=$1; v=$2; shift 2
      case "$p" in
        power)    echo "$v" > "$state/power"; [ "$v" = 1 ] && date +%s > "$state/booted" ;;
        reset)    date +%s > "$state/booted" ;;
        boot)     echo "$v" > "$state/boot" ;;
        shutdown) ( sleep 3; echo 0 > "$state/power" ) >/dev/null 2>&1 & ;;
      esac
    done ;;
esac
exit 0
