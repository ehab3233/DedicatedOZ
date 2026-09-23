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
import contextlib
import getpass
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

from app.drivers.cimc import CimcXmlApi
from app.drivers.ipmi import IpmiDriver
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


def test_pxe_boot(driver: RedfishDriver, ipmi: IpmiDriver, *, power_cycle: bool) -> Result:
    banner("2/5  One-time PXE boot override (IPMI first, then Redfish)")
    notes: list[str] = []
    paths: dict[str, bool] = {}

    # The platform sets boot device over IPMI first and Redfish second, and
    # reads it back either way. Test both, so you know which one you rely on.
    for label, target in (("IPMI", ipmi), ("Redfish", driver)):
        try:
            target.set_boot_once("pxe")
            paths[label] = True
            print(f"  {GREEN}{label}: override set and read back{RESET}")
        except Exception as exc:  # noqa: BLE001
            paths[label] = False
            print(f"  {RED}{label}: {exc}{RESET}")
    if not any(paths.values()):
        return Result(
            "pxe_override",
            False,
            "neither IPMI nor Redfish could set a one-time PXE boot",
            ["Run with --prepare to switch IPMI over LAN on, or check the CIMC "
             "user is an administrator. Without this, installs go through vMedia."],
        )
    for label, ok in paths.items():
        if not ok:
            notes.append(f"{label} could not set the boot device; the other path will be used")

    if not power_cycle:
        return Result(
            "pxe_override",
            True,
            "override set via " + " and ".join(k for k, v in paths.items() if v),
            notes + ["re-run with --power-cycle to confirm the machine actually netboots"],
        )

    print("  Power cycling...")
    (ipmi if paths.get("IPMI") else driver).power_cycle()
    print("  Waiting for power on...")
    if not (ipmi if paths.get("IPMI") else driver).wait_for_power_state("on", timeout=180):
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
    return Result("pxe_override", True, "machine netbooted on a one-time override", notes)


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


def test_console(host: str, credential: BMCCredential, ipmi: IpmiDriver) -> Result:
    banner("4/5  IPMI power path, Serial-over-LAN, and vKVM")
    notes: list[str] = []

    if shutil.which("ipmitool") is None:
        return Result("console", False, "ipmitool is not installed on this machine")

    print("  IPMI over LAN (the platform's primary power path)")
    try:
        started = time.monotonic()
        state = ipmi.power_status().state
        took = time.monotonic() - started
        print(f"  {GREEN}power is {state}, read in {took * 1000:.0f} ms{RESET}")
        if took > 3:
            notes.append(
                f"IPMI reads take {took:.1f}s. Try DOZ_IPMI_CIPHER_SUITE= (empty, auto) or 17."
            )
    except Exception as exc:  # noqa: BLE001
        return Result(
            "console",
            False,
            f"IPMI failed: {exc}",
            ["Run with --prepare (or CIMC web UI > Admin > Communication Services > "
             "IPMI over LAN). The serial console and the fast power path need it."],
        )

    try:
        info = ipmi.sol_info()
        print(f"  SOL enabled: {info.get('Enabled')}, "
              f"bit rate: {info.get('Non-Volatile Bit Rate (kbps)')} kbps")
        if info.get("Enabled", "").lower() != "true":
            notes.append("SOL is disabled. Run with --prepare, or Prepare BMC in the panel.")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"could not read SOL settings: {exc}")

    # Exactly how the console bridge runs it: ipmitool on a pty.
    ipmi.sol_deactivate()
    print("  Opening a SOL session on a pty for 10 seconds (press nothing)")
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(  # noqa: S603
            [*ipmi.base_command(), "-e", "\x1d", "sol", "activate"],
            stdin=slave, stdout=slave, stderr=slave, env=ipmi.environment(),
            start_new_session=True, close_fds=True,
        )
    except OSError as exc:
        return Result("console", False, f"could not start ipmitool: {exc}")
    os.close(slave)
    output = b""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.25)
        if ready:
            try:
                output += os.read(master, 4096)
            except OSError:
                break
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    os.close(master)
    ipmi.sol_deactivate()
    text = output.decode(errors="replace")

    if "already active" in text:
        return Result("console", False, "SOL payload already active",
                      ["Another session holds the console; close it and re-run."])
    if "SOL Session operational" not in text:
        return Result("console", False, f"SOL did not start: {text.strip()[:200]}")
    print(f"  {GREEN}SOL session opened and released cleanly{RESET}")
    if len(text.strip()) < 60:
        notes.append(
            "SOL produced no host output. Expected if the server is off or sitting at "
            "a login prompt; with it booting you should see POST. If you never do, "
            "BIOS console redirection is off -- --prepare sets it (next boot)."
        )

    print("\n  vKVM: asking the CIMC for one-time launch tokens")
    try:
        with CimcXmlApi(host, credential) as api:
            links = api.kvm_launch()
            print(f"  XML API ok (firmware {api.version})")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"KVM tokens unavailable ({exc}); the CIMC web UI still works by hand")
        return Result("console", True, "IPMI and SOL work; KVM tokens did not", notes)

    if links["html5"]:
        print("  HTML5 viewer found. Open this in a browser within a minute:")
        print(f"    {links['html5']}")
    else:
        print("  No HTML5 viewer at any known path. Java launcher:\n    " + links["java"])
        notes.append("set DOZ_KVM_URL_TEMPLATE once you know your firmware's HTML5 viewer URL")
    answer = input("  Did the KVM viewer open and show video? [y/N/skip] ").strip().lower()
    if answer == "skip":
        notes.append("vKVM not tested")
    elif answer != "y":
        notes.append(
            "The KVM link did not work. The panel's CIMC web UI link still does; "
            "please note what URL the CIMC's own Launch KVM button opens and set "
            "DOZ_KVM_URL_TEMPLATE to match."
        )
    return Result("console", True, "IPMI, SOL and KVM tokens work", notes)


def prepare_bmc(host: str, credential: BMCCredential) -> None:
    """What the panel's Prepare BMC job does, for a CIMC not yet in the panel."""
    banner("Preparing the CIMC (IPMI over LAN, SOL, console redirection)")
    with CimcXmlApi(host, credential) as api:
        for label, fn in (
            ("IPMI over LAN", api.enable_ipmi_over_lan),
            ("Serial-over-LAN 115200 COM0", api.enable_sol),
            ("BIOS console redirection (next boot)", api.set_console_redirection),
        ):
            try:
                fn()
                print(f"  {GREEN}{label}: set{RESET}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {RED}{label}: {exc}{RESET}")
    time.sleep(3)


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
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="first switch on IPMI over LAN, SOL and BIOS console redirection "
             "(what the panel's Prepare BMC does)",
    )
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
    ipmi = IpmiDriver(
        args.host, credential, log=verbose_sink if args.verbose else (lambda *a, **k: None)
    )

    results: list[Result] = []
    try:
        if args.prepare:
            prepare_bmc(args.host, credential)
        results.append(test_firmware(driver))
        results.append(test_pxe_boot(driver, ipmi, power_cycle=args.power_cycle))
        if args.power_cycle:
            results.append(test_virtual_media(driver, args.iso_url))
        else:
            print(f"\n{YELLOW}3/5 virtual media skipped without --power-cycle{RESET}")
            results.append(Result("virtual_media", False, "skipped (no --power-cycle)"))
        results.append(test_console(args.host, credential, ipmi))
        results.append(test_storcli())
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    finally:
        # Never leave a bench machine with a boot override set.
        for cleanup in (ipmi.clear_boot_override, driver.clear_boot_override,
                        driver.eject_virtual_media):
            with contextlib.suppress(Exception):
                cleanup()
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
