#!/usr/bin/env python3
"""Pre-build hardware validation — the five bench tests from section 2 of the spec.

Run this against one server on the bench, before racking anything and before
trusting any of the provisioning code. If all five pass, the architecture is
safe to build on. If step 2 or 3 fails, the whole netboot design has to change,
and it is much cheaper to learn that now.

    python -m scripts.bench_validate --host 10.0.0.10 --user admin \
        --iso-url http://10.10.0.5:8080/iso/ubuntu-22.04.iso

Talks to the BMC directly. No database, no queue, no control plane.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

from app.drivers.redfish import RedfishDriver
from app.secrets import BMCCredential

#: The last firmware release for the M4. Everything should be standardised here.
TARGET_FIRMWARE = "4.1(2f)"
#: First release with Redfish 1.0.1 and the HTML5 vKVM.
MINIMUM_FIRMWARE_MAJOR = 3

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


@dataclass
class Result:
    name: str
    passed: bool
    detail: str = ""
    notes: list[str] = field(default_factory=list)


def banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def verbose_sink(message: str, *, level="info", request=None, response=None) -> None:  # noqa: ANN001
    colour = {"error": RED, "warning": YELLOW}.get(level, DIM)
    print(f"  {colour}{message}{RESET}")


# ---------------------------------------------------------------------------
# Test 1 — firmware
# ---------------------------------------------------------------------------


def test_firmware(driver: RedfishDriver) -> Result:
    banner("1/5  CIMC firmware and Redfish reachability")
    root = driver.service_root()
    redfish_version = root.get("RedfishVersion", "unknown")
    print(f"  Redfish version : {redfish_version}")

    inv = driver.inventory()
    firmware = inv.bmc_firmware or "unknown"
    print(f"  CIMC firmware   : {firmware}")
    print(f"  Model           : {inv.model}")
    print(f"  Serial          : {inv.serial}")
    print(f"  BIOS            : {inv.bios_version}")
    print(f"  CPU             : {inv.cpu_count}x {inv.cpu_model}")
    print(f"  RAM             : {inv.ram_gb} GB")
    print(f"  NICs            : {len(inv.nics)}")
    for nic in inv.nics:
        print(f"                    {nic['mac']}  {nic.get('name')}  {nic.get('link_status')}")
    print(f"  Drives          : {len(inv.drives)}")

    notes = []
    passed = True

    if firmware == "unknown":
        return Result("firmware", False, "BMC did not report a firmware version")

    major = firmware.split(".")[0]
    if major.isdigit() and int(major) < MINIMUM_FIRMWARE_MAJOR:
        passed = False
        notes.append(
            f"firmware {firmware} predates Redfish support; upgrade via HUU on the "
            "bench before racking"
        )
    elif firmware != TARGET_FIRMWARE:
        notes.append(
            f"firmware is {firmware}, not the fleet target {TARGET_FIRMWARE}. "
            "Standardise before racking -- a mixed fleet makes every later "
            "failure ambiguous."
        )

    if not inv.nics:
        passed = False
        notes.append(
            "no NIC MACs discoverable over Redfish. The netboot rail keys on the "
            "PXE MAC, so it would have to be recorded by hand for every server."
        )

    return Result("firmware", passed, f"CIMC {firmware}, Redfish {redfish_version}", notes)


# ---------------------------------------------------------------------------
# Test 2 — one-time PXE boot
# ---------------------------------------------------------------------------


def test_pxe_boot(driver: RedfishDriver, *, power_cycle: bool) -> Result:
    banner("2/5  One-time PXE boot override via Redfish")
    print("  Setting BootSourceOverrideTarget=Pxe, Enabled=Once")
    try:
        driver.set_boot_once("pxe")
    except Exception as exc:  # noqa: BLE001
        return Result(
            "pxe_override",
            False,
            str(exc),
            [
                "If the PATCH is silently ignored, the install rail cannot work "
                "and provisioning must go through vMedia instead."
            ],
        )

    print(f"  {GREEN}override accepted and read back correctly{RESET}")

    if not power_cycle:
        return Result(
            "pxe_override",
            True,
            "override set (not power cycled)",
            ["re-run with --power-cycle to confirm the machine actually netboots"],
        )

    print("  Power cycling...")
    driver.power_cycle()
    print("  Waiting for power on...")
    if not driver.wait_for_power_state("on", timeout=180):
        return Result("pxe_override", False, "server did not power on within 180s")

    print(f"\n  {YELLOW}Watch the serial console now.{RESET}")
    print("  You are looking for the NIC's PXE ROM and a DHCP request.")
    answer = input("  Did the machine attempt a network boot? [y/N] ").strip().lower()
    if answer != "y":
        return Result(
            "pxe_override",
            False,
            "operator reports no netboot attempt",
            [
                "Check that the NIC has PXE enabled in BIOS and that the "
                "provisioning VLAN reaches a DHCP server.",
                "If PXE cannot be made reliable, vMedia (test 3) becomes the "
                "primary install path.",
            ],
        )
    return Result("pxe_override", True, "machine netbooted on a one-time override")


# ---------------------------------------------------------------------------
# Test 3 — virtual media
# ---------------------------------------------------------------------------


def test_virtual_media(driver: RedfishDriver, iso_url: str | None) -> Result:
    banner("3/5  Virtual media (ISO over HTTP)")
    if not iso_url:
        print(f"  {YELLOW}skipped: pass --iso-url to run this test{RESET}")
        return Result(
            "virtual_media",
            False,
            "skipped",
            ["This is the fallback install path. Do not skip it before racking."],
        )

    print(f"  Inserting {iso_url}")
    try:
        driver.insert_virtual_media(iso_url)
    except Exception as exc:  # noqa: BLE001
        return Result(
            "virtual_media",
            False,
            str(exc),
            [
                "CIMC vMedia is fussy about URLs: it needs a plain HTTP path with "
                "no redirect and no authentication, and it will fail silently on "
                "an HTTPS URL whose certificate it cannot validate.",
            ],
        )

    print(f"  {GREEN}media reports as inserted{RESET}")
    print("  Setting one-time boot to CD and power cycling")
    driver.set_boot_once("cd")
    driver.power_cycle()

    if not driver.wait_for_power_state("on", timeout=180):
        driver.eject_virtual_media()
        return Result("virtual_media", False, "server did not power on")

    print(f"\n  {YELLOW}Watch the serial console.{RESET}")
    answer = input("  Did the machine boot the ISO? [y/N] ").strip().lower()

    print("  Ejecting media")
    driver.eject_virtual_media()

    if answer != "y":
        return Result(
            "virtual_media",
            False,
            "operator reports the ISO did not boot",
            ["Check the ISO is hybrid/El Torito bootable and served without redirects."],
        )
    return Result("virtual_media", True, "ISO mounted and booted over HTTP")


# ---------------------------------------------------------------------------
# Test 4 — SOL and vKVM
# ---------------------------------------------------------------------------


def test_console(host: str, credential: BMCCredential) -> Result:
    banner("4/5  Serial-over-LAN and vKVM")
    notes: list[str] = []

    if shutil.which("ipmitool") is None:
        return Result("console", False, "ipmitool is not installed on this machine")

    env = {**os.environ, "IPMI_PASSWORD": credential.password}
    base = ["ipmitool", "-I", "lanplus", "-H", host, "-U", credential.username, "-E"]

    print("  Checking IPMI 2.0 reachability (chassis status)")
    status = subprocess.run(  # noqa: S603
        [*base, "chassis", "status"], capture_output=True, text=True, timeout=30, env=env
    )
    if status.returncode != 0:
        return Result(
            "console",
            False,
            f"ipmitool chassis status failed: {status.stderr.strip()}",
            ["IPMI over LAN may be disabled in the CIMC; the SOL console needs it."],
        )
    print(f"  {GREEN}IPMI reachable{RESET}")

    # Clear any session left behind by an earlier run -- the CIMC allows only
    # one SOL session and will refuse the next connect until it is released.
    subprocess.run(  # noqa: S603
        [*base, "sol", "deactivate"], capture_output=True, text=True, timeout=30, env=env
    )

    print("  Opening a SOL session for 10 seconds")
    try:
        sol = subprocess.Popen(  # noqa: S603
            [*base, "sol", "activate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            env=env,
        )
    except OSError as exc:
        return Result("console", False, f"could not start ipmitool: {exc}")

    time.sleep(10)
    sol.terminate()
    output = ""
    try:
        output = sol.communicate(timeout=10)[0] or ""
    except subprocess.TimeoutExpired:
        sol.kill()
    subprocess.run(  # noqa: S603
        [*base, "sol", "deactivate"], capture_output=True, text=True, timeout=30, env=env
    )

    if "SOL payload already active" in output:
        return Result(
            "console",
            False,
            "SOL payload already active",
            ["Another session holds the console. The platform must always "
             "`sol deactivate` when a websocket closes, or consoles wedge."],
        )
    if "Error" in output and "activate" in output.lower():
        return Result("console", False, f"SOL activate failed: {output.strip()[:200]}")

    print(f"  {GREEN}SOL session opened and released cleanly{RESET}")
    if not output.strip():
        notes.append(
            "SOL produced no output. That is expected on a powered-off machine, "
            "but confirm you see POST output with the server running."
        )

    print(f"\n  {DIM}vKVM cannot be tested headlessly. Open the CIMC web UI and{RESET}")
    print(f"  {DIM}launch the HTML5 KVM console manually.{RESET}")
    answer = input("  Does the HTML5 vKVM launch and show video? [y/N/skip] ").strip().lower()
    if answer == "skip":
        notes.append("vKVM not tested; it is a Phase 2 feature, so this is survivable")
    elif answer != "y":
        notes.append(
            "vKVM does not work. Phase 2 proxying will not be possible without it; "
            "SOL then has to carry all console support."
        )

    return Result("console", True, "SOL works", notes)


# ---------------------------------------------------------------------------
# Test 5 — StorCLI
# ---------------------------------------------------------------------------


def test_storcli() -> Result:
    banner("5/5  StorCLI non-interactive control")
    print(
        "  This one has to run ON the server, from a live Linux image -- StorCLI\n"
        "  talks to the RAID controller over PCIe, not over the network. The\n"
        "  spec calls this the most awkward of the five, and it is.\n"
    )
    print(f"  {DIM}Boot a live image (test 2 or 3 just proved you can) and run:{RESET}\n")
    for command in [
        "storcli64 /c0 show all              # controller present, firmware level",
        "storcli64 /c0/eall/sall show        # enclosure:slot for every drive",
        "storcli64 /c0/vall show             # existing virtual drives",
        "storcli64 /c0/vall delete force     # destroy them",
        "storcli64 /c0/eall/sall set good force",
        "storcli64 /c0 add vd type=raid1 drives=252:1,252:2 wb ra direct",
        "storcli64 /c0/v0 set bootdrive=on",
        "storcli64 /c0 set jbod=on           # needed before any per-drive erase",
    ]:
        print(f"    {command}")

    print(
        f"\n  {YELLOW}What you are checking:{RESET}\n"
        "    * every command completes without prompting for confirmation\n"
        "    * the enclosure ID matches what the script expects (usually 252)\n"
        "    * a new VD appears as /dev/sda within ~30 seconds\n"
        "    * JBOD mode is actually supported -- without it, secure erase\n"
        "      cannot reach the individual drives and wipe falls back to a\n"
        "      full overwrite, which changes your deprovision time budget\n"
    )
    answer = input("  Did all of the above work non-interactively? [y/N/skip] ").strip().lower()
    if answer == "skip":
        return Result("storcli", False, "skipped", ["Do not skip this. Budget time for it."])
    if answer != "y":
        return Result(
            "storcli",
            False,
            "operator reports StorCLI problems",
            [
                "The ramdisk drives StorCLI non-interactively for every install "
                "and every wipe. If it prompts or the enclosure ID differs, fix "
                "installer/templates/ramdisk/provision.sh.j2 before going further."
            ],
        )
    return Result("storcli", True, "StorCLI driveable non-interactively")


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="CIMC IP or hostname")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default=None, help="prompted for if omitted")
    parser.add_argument("--iso-url", default=None, help="plain HTTP URL to a bootable ISO")
    parser.add_argument(
        "--power-cycle",
        action="store_true",
        help="actually reboot the server during tests 2 and 3",
    )
    parser.add_argument("--verify-tls", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="print every BMC exchange")
    args = parser.parse_args()

    password = args.password or getpass.getpass(f"CIMC password for {args.user}@{args.host}: ")
    credential = BMCCredential(username=args.user, password=password)

    if not args.power_cycle:
        print(
            f"\n{YELLOW}Running in read-mostly mode. Tests 2 and 3 will not reboot the\n"
            f"server, so they cannot fully confirm the boot path. Re-run with\n"
            f"--power-cycle on a machine you are willing to reboot.{RESET}"
        )

    driver = RedfishDriver(
        host=args.host,
        credential=credential,
        log=verbose_sink if args.verbose else (lambda *a, **k: None),
        verify_tls=args.verify_tls,
    )

    results: list[Result] = []
    try:
        results.append(test_firmware(driver))
        results.append(test_pxe_boot(driver, power_cycle=args.power_cycle))
        if args.power_cycle:
            results.append(test_virtual_media(driver, args.iso_url))
        else:
            print(f"\n{YELLOW}3/5 virtual media skipped without --power-cycle{RESET}")
            results.append(Result("virtual_media", False, "skipped (no --power-cycle)"))
        results.append(test_console(args.host, credential))
        results.append(test_storcli())
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    finally:
        # Never leave a bench machine with a boot override set.
        try:
            driver.clear_boot_override()
            driver.eject_virtual_media()
        except Exception:  # noqa: BLE001
            print(f"{YELLOW}warning: could not clear boot override / eject media{RESET}")
        driver.close()

    banner("Summary")
    for result in results:
        mark = f"{GREEN}PASS{RESET}" if result.passed else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {result.name:<16} {result.detail}")
        for note in result.notes:
            print(f"        {YELLOW}note:{RESET} {note}")

    failures = [r for r in results if not r.passed]
    if failures:
        print(
            f"\n{RED}{len(failures)} of {len(results)} checks did not pass.{RESET}\n"
            "Resolve these before racking the fleet. The design assumes all five."
        )
        return 1

    print(f"\n{GREEN}All five checks passed. The provisioning design is safe to build on.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
