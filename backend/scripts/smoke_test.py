#!/usr/bin/env python3
"""End-to-end check of an installed management server.

Run by deploy/smoke-test.sh after an install. Talks to the platform the way a
browser does -- through nginx on port 80 -- and drives the BMC simulator
registered as SIM-0001:

  1. log in as an admin
  2. read live power state
  3. force off, power on, reset, power cycle -- each as a job, each confirmed
     against the simulator's own state
  4. read sensors, the event log and the BMC's own details; blink the locator
     LED; set a one-time boot device as a job
  5. open the serial console over the websocket, type, and read the answer

Exits non-zero on the first failure, saying what failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import requests
import websockets

OK, FAIL = "\033[32mok\033[0m", "\033[31mFAIL\033[0m"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--serial", default="SIM-0001")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    def step(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {label:<44} {OK if ok else FAIL} {detail}")
        if not ok:
            raise SystemExit(1)

    print("== health ==")
    health = requests.get(f"{base}/health", timeout=10).json()
    step("API through nginx", health.get("status") == "ok", str(health.get("database")))

    login = requests.post(f"{base}/api/v1/auth/login",
                          json={"email": args.email, "password": args.password}, timeout=10)
    step("admin login", login.status_code == 200, str(login.status_code))
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    system = requests.get(f"{base}/api/v1/admin/system", headers=headers, timeout=15).json()
    for queue, info in system["queues"].items():
        step(f"worker on the {queue} queue", bool(info["workers"]), ", ".join(info["workers"]))
    ipmitool = system.get("ipmitool") or {}
    step("ipmitool present", bool(ipmitool), ipmitool.get("version", ""))

    fleet = requests.get(f"{base}/api/v1/admin/servers", headers=headers, timeout=10).json()
    server = next((s for s in fleet if s["serial"] == args.serial), None)
    step(f"{args.serial} registered", server is not None)
    sid = server["id"]

    print("== power ==")
    state = requests.get(
        f"{base}/api/v1/servers/{sid}/power?fresh=1", headers=headers, timeout=30
    ).json()
    step("live power state", state["state"] in {"on", "off"},
         f"{state['state']} via {state['via']}")

    for action, want in [("force_off", "off"), ("on", "on"), ("reset", "on"), ("cycle", "on")]:
        r = requests.post(f"{base}/api/v1/servers/{sid}/power", headers=headers,
                          json={"action": action}, timeout=10)
        step(f"queue {action}", r.status_code == 202, str(r.status_code))
        job_id, started = r.json()["id"], time.monotonic()
        while True:
            job = requests.get(
                f"{base}/api/v1/admin/jobs/{job_id}", headers=headers, timeout=10
            ).json()
            finished = job["state"] in {"succeeded", "failed", "cancelled"}
            if finished or time.monotonic() - started > 90:
                break
            time.sleep(0.5)
        took = time.monotonic() - started
        detail = (
            f"{took:.1f}s, power {job['result'].get('power_state')} via {job['result'].get('via')}"
            if job["state"] == "succeeded"
            else (job.get("error") or job["state"])
        )
        step(f"{action} job", job["state"] == "succeeded", detail)
        live = requests.get(
            f"{base}/api/v1/servers/{sid}/power?fresh=1", headers=headers, timeout=30
        ).json()
        step(f"  BMC reports power {want}", live["state"] == want, live["state"])

    print("== sensors, event log, BMC ==")
    sensors = requests.get(f"{base}/api/v1/admin/servers/{sid}/sensors?fresh=1",
                           headers=headers, timeout=60).json()
    numeric = [x for x in sensors.get("sensors", []) if x.get("value") is not None]
    first = numeric[0] if numeric else {}
    example = f"{first.get('name')} = {first.get('value')} {first.get('unit')}" if first else ""
    step("sensor readings over IPMI", len(numeric) > 0,
         f"{len(sensors.get('sensors', []))} sensors, e.g. {example}")
    kinds = {x["kind"] for x in numeric}
    step("temperatures, fans and voltages present", {"temperature", "fan", "voltage"} <= kinds,
         ", ".join(sorted(kinds)))
    sel = requests.get(f"{base}/api/v1/admin/servers/{sid}/sel?fresh=1", headers=headers,
                       timeout=60).json()
    step("event log readable", "entries" in sel and "info" in sel,
         f"{sel.get('info', {}).get('entries')} entries")
    info = requests.get(f"{base}/api/v1/admin/servers/{sid}/bmc/info?fresh=1", headers=headers,
                        timeout=60).json()
    step("BMC firmware, LAN and chassis state", bool(info.get("mc", {}).get("firmware"))
         and "power_restore_policy" in info.get("chassis", {}),
         f"firmware {info.get('mc', {}).get('firmware')}, "
         f"policy {info.get('chassis', {}).get('power_restore_policy')}")
    led = requests.post(f"{base}/api/v1/admin/servers/{sid}/identify", headers=headers,
                        json={"seconds": 5}, timeout=30)
    step("locator LED", led.status_code == 200, led.text[:60])
    fleet_power = requests.get(f"{base}/api/v1/admin/power", headers=headers, timeout=60).json()
    mine = fleet_power.get("servers", {}).get(sid, {})
    step("fleet power read", mine.get("state") in {"on", "off"},
         f"{mine.get('state')} via {mine.get('via')}")

    r = requests.post(f"{base}/api/v1/admin/servers/{sid}/boot", headers=headers,
                      json={"device": "pxe", "then": "reset"}, timeout=10)
    step("queue boot once from PXE", r.status_code == 202, str(r.status_code))
    job_id, started = r.json()["id"], time.monotonic()
    while True:
        job = requests.get(f"{base}/api/v1/admin/jobs/{job_id}", headers=headers,
                           timeout=10).json()
        if job["state"] in {"succeeded", "failed", "cancelled"} or time.monotonic() - started > 90:
            break
        time.sleep(0.5)
    step("boot override job", job["state"] == "succeeded",
         job.get("stage") if job["state"] == "succeeded" else (job.get("error") or job["state"]))

    print("== serial console ==")

    async def console() -> tuple[list[str], str]:
        ticket = requests.post(f"{base}/api/v1/console/{sid}/ticket", headers=headers,
                               timeout=10).json()["ticket"]
        ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
        states: list[str] = []
        output = b""
        url = f"{ws_base}/api/v1/console/{sid}/sol?ticket={ticket}&force=1"
        async with websockets.connect(url) as ws:
            async def read():
                nonlocal output
                async for message in ws:
                    if isinstance(message, bytes):
                        output += message
                    else:
                        states.append(json.loads(message)["state"])

            reader = asyncio.create_task(read())
            for _ in range(40):
                if "connected" in states or "busy" in states:
                    break
                await asyncio.sleep(0.25)
            await ws.send(b"\r")
            await asyncio.sleep(0.5)
            await ws.send(b"uname -a\r")
            await asyncio.sleep(2)
            reader.cancel()
        return states, output.decode(errors="replace")

    states, text = asyncio.run(console())
    step("websocket through nginx, SOL connected", "connected" in states, " -> ".join(states))
    step("typed command answered by the host", "Linux web01" in text,
         repr(text[-80:]) if "Linux web01" not in text else "")
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
