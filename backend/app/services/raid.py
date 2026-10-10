"""RAID on the server's own controller, configured through its BMC.

The C220 M4's MegaRAID controller is managed by the CIMC out of band, so a
virtual drive can be built before the server boots anything, from the
management server, with no StorCLI in the ramdisk. Redfish (Storage ->
Volumes) is tried first; firmware that does not take volume writes falls
back to the CIMC XML API, whose storage objects go back to CIMC 2.0.

Everything here destroys data on the drives it touches. Callers confirm
that before they get here.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

from app.config import settings
from app.drivers.base import BMCError, LogSink, null_sink
from app.drivers.cimc import CimcXmlApi
from app.drivers.factory import credential_for
from app.drivers.redfish import RedfishDriver
from app.enums import PowerAction, RaidLevel
from app.models import Server

VOLUME_NAME = "doz"

MIN_DRIVES = {
    RaidLevel.RAID0: 1, RaidLevel.RAID1: 2, RaidLevel.RAID5: 3,
    RaidLevel.RAID6: 4, RaidLevel.RAID10: 4,
}
REDFISH_TYPE = {
    RaidLevel.RAID0: "RAID0", RaidLevel.RAID1: "RAID1", RaidLevel.RAID5: "RAID5",
    RaidLevel.RAID6: "RAID6", RaidLevel.RAID10: "RAID10",
}
XML_LEVEL = {
    RaidLevel.RAID0: 0, RaidLevel.RAID1: 1, RaidLevel.RAID5: 5,
    RaidLevel.RAID6: 6, RaidLevel.RAID10: 10,
}
#: How a CIMC names a level: Redfish RAIDType, Redfish 1.0 VolumeType and
#: the XML API's raidLevel all occur in the field.
_LEVEL_NAMES = {
    RaidLevel.RAID0: {"raid0", "raid 0", "nonredundant"},
    RaidLevel.RAID1: {"raid1", "raid 1", "mirrored"},
    RaidLevel.RAID5: {"raid5", "raid 5", "stripedwithparity"},
    RaidLevel.RAID6: {"raid6", "raid 6"},
    RaidLevel.RAID10: {"raid10", "raid 10", "spannedmirrors"},
}
#: Controllers that cannot build an array (the chipset SATA ports).
_NOT_RAID = re.compile(r"ahci|sata|pch|nvme", re.IGNORECASE)
_RAID = re.compile(r"raid|mraid|megaraid|sas", re.IGNORECASE)


class RaidError(RuntimeError):
    pass


def usable_capacity(level: RaidLevel, sizes: list[int | None]) -> int | None:
    """Bytes a virtual drive of `level` over drives of `sizes` can hold, or
    None when the BMC did not report every drive's size."""
    if not sizes or any(not s for s in sizes):
        return None
    known = [int(s) for s in sizes if s]
    n, smallest = len(known), min(known)
    return {
        RaidLevel.RAID0: sum(known),
        RaidLevel.RAID1: smallest,
        RaidLevel.RAID5: (n - 1) * smallest,
        RaidLevel.RAID6: (n - 2) * smallest,
        RaidLevel.RAID10: (n // 2) * smallest,
    }[level]


#: Drive states (Redfish Status.State and Cisco's DriveState) that cannot
#: join an array. Anything else is tried: a BMC that reports something new
#: is better answered by the controller than by a guess here.
_UNUSABLE_STATES = {
    "absent", "disabled", "unavailableoffline", "failed", "unconfiguredbad",
    "unconfigured bad", "foreign", "foreignconfiguration", "predictivefailure",
}


def describe_drive(d: dict) -> str:
    cap = d.get("capacity_bytes")
    return (
        f"{d.get('name') or d.get('id')}: "
        f"{f'{int(cap) / 1_000_000_000:.0f} GB' if cap else 'size unknown'} "
        f"{d.get('media') or ''}, health {d.get('health') or 'unknown'}, "
        f"state {d.get('oem_state') or d.get('state') or 'unknown'}"
        + (", failure predicted" if d.get("failure_predicted") else "")
    )


def unusable_reason(d: dict) -> str | None:
    if d.get("failure_predicted"):
        return "failure predicted"
    if str(d.get("health") or "").lower() in ("critical", "severe fault"):
        return f"health {d.get('health')}"
    for key in ("state", "oem_state"):
        value = str(d.get(key) or "").replace(" ", "").lower()
        if value in _UNUSABLE_STATES or value.replace(" ", "") in _UNUSABLE_STATES:
            return f"state {d.get(key)}"
    return None


def choose_drives(level: RaidLevel, drives: list[dict]) -> list[dict]:
    """Pick the member drives: healthy ones, matched by media and size.

    RAID 1 takes the first two of the largest group of identical drives;
    RAID 10 takes an even number of them; the rest take every usable drive.
    """
    if level not in MIN_DRIVES:
        raise RaidError(f"{level.value} is not a RAID level that can be built")
    eligible = [d for d in drives if unusable_reason(d) is None]
    eligible.sort(key=lambda d: (d.get("media") or "", -int(d.get("capacity_bytes") or 0)))
    need = MIN_DRIVES[level]
    if len(eligible) < need:
        detail = "; ".join(
            f"{describe_drive(d)}" + (f" ({unusable_reason(d)})" if unusable_reason(d) else "")
            for d in drives
        )
        raise RaidError(
            f"{level.value} needs {need} drives; {len(eligible)} usable of {len(drives)} "
            f"found. Drives: {detail or 'none reported'}"
        )
    if level in (RaidLevel.RAID1, RaidLevel.RAID10):
        groups: dict[tuple, list[dict]] = {}
        for d in eligible:
            groups.setdefault((d.get("media"), d.get("capacity_bytes")), []).append(d)
        best = max(groups.values(), key=len)
        if level is RaidLevel.RAID1:
            return best[:2] if len(best) >= 2 else eligible[:2]
        members = best if len(best) >= 4 else eligible
        return members[: len(members) - (len(members) % 2)]
    return eligible


def level_matches(level: RaidLevel, name: str | None) -> bool:
    return (name or "").strip().lower() in _LEVEL_NAMES.get(level, set())


def _redfish_drive_is_jbod(d: dict) -> bool:
    return "jbod" in str(d.get("oem_state") or d.get("state") or "").lower()


def describe(level: RaidLevel, drives: list[dict], capacity_bytes: int | None) -> str:
    names = ", ".join(str(d.get("name") or d.get("id")) for d in drives)
    size = f", {capacity_bytes / 1_000_000_000:.0f} GB" if capacity_bytes else ""
    return f"{level.value.upper()} over {len(drives)} drives ({names}){size}"


def _is_raid_controller(c: dict) -> bool:
    name = " ".join(filter(None, [c.get("name"), c.get("model")]))
    return bool(c.get("raid_types")) or bool(_RAID.search(name) and not _NOT_RAID.search(name))


def ensure_host_on(redfish: RedfishDriver, log: LogSink) -> None:
    """Power the host on if it is off, and wait for the RAID controller.

    The controller is a PCIe card: it only runs while the host has power.
    With the host off the CIMC lists it as Disabled and answers every
    storage write with "storage subsystem not ready yet".
    """
    try:
        state = redfish.power_status().state
    except BMCError as exc:
        log(f"could not read the power state ({exc}); carrying on", level="warning")
        return
    if state == "on":
        return
    log("the host is powered off and the RAID controller only runs while it is on; "
        "powering on (a PXE boot during this is told to wait)")
    redfish.power(PowerAction.ON)
    deadline = time.monotonic() + settings.raid_bmc_timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(10)
        try:
            controllers = [c for c in redfish.storage() if _is_raid_controller(c)]
        except BMCError as exc:
            log(f"waiting for the RAID controller: {exc}", level="warning")
            continue
        if controllers and all((c.get("state") or "Enabled") != "Disabled" for c in controllers):
            log(f"RAID controller is up ({controllers[0]['name']})")
            return
    log("the RAID controller has not reported ready; trying anyway", level="warning")


def _until_ready(fn: Callable[[], dict], *, log: LogSink, what: str) -> dict:
    """Run a CIMC storage write, retrying while it says the subsystem is not
    ready (error 2003): the controller is still initialising after power on."""
    deadline = time.monotonic() + settings.raid_bmc_timeout_seconds
    while True:
        try:
            return fn()
        except BMCError as exc:
            text = str(exc).lower()
            if "not ready" not in text and "error 2003" not in text:
                raise
            if time.monotonic() >= deadline:
                raise BMCError(f"{what}: the storage subsystem never became ready") from exc
            log(f"{what}: storage subsystem not ready yet; retrying in 10 s", level="warning")
            time.sleep(10)


def configure(server: Server, level: RaidLevel, *, log: LogSink = null_sink) -> dict:
    """Replace whatever the controller holds with one `level` virtual drive."""
    credential = credential_for(server)
    host = str(server.cimc_ip)
    errors: list[str] = []
    redfish = RedfishDriver(
        host=host, credential=credential, port=server.redfish_port,
        system_path=server.redfish_system_path, log=log,
    )
    ensure_host_on(redfish, log)

    def xml_api() -> CimcXmlApi:
        return CimcXmlApi(host, credential, port=server.redfish_port, log=log,
                          timeout=settings.raid_bmc_timeout_seconds)

    try:
        return _configure_redfish(redfish, level, log, xml_api=xml_api)
    except BMCError as exc:
        errors.append(f"Redfish: {exc}")
        log(f"Redfish could not build the array ({exc}); trying the CIMC XML API",
            level="warning")
    try:
        with CimcXmlApi(host, credential, port=server.redfish_port, log=log) as api:
            return _configure_xml(api, level, log)
    except BMCError as exc:
        errors.append(f"CIMC XML API: {exc}")
    raise RaidError("the BMC could not build the array. " + "; ".join(errors))


def clear(server: Server, *, log: LogSink = null_sink) -> dict:
    """Delete every virtual drive, leaving the disks unconfigured."""
    credential = credential_for(server)
    host = str(server.cimc_ip)
    redfish = RedfishDriver(
        host=host, credential=credential, port=server.redfish_port,
        system_path=server.redfish_system_path, log=log,
    )
    ensure_host_on(redfish, log)
    try:
        controllers = [c for c in redfish.storage() if c["volumes"]]
        for controller in controllers:
            for volume in controller["volumes"]:
                log(f"deleting virtual drive {volume.get('name')} on {controller['name']}")
                redfish.delete_volume(volume["path"])
        return {"via": "redfish", "deleted": sum(len(c["volumes"]) for c in controllers),
                "description": "every virtual drive deleted"}
    except BMCError as exc:
        log(f"Redfish could not delete the virtual drives ({exc}); trying the CIMC XML API",
            level="warning")
    with CimcXmlApi(host, credential, port=server.redfish_port, log=log,
                    timeout=settings.raid_bmc_timeout_seconds) as api:
        deleted = 0
        for controller in api.storage_controllers():
            vds = api.virtual_drives(controller["dn"])
            if any(vd.get("bootDrive") == "true" for vd in vds):
                _until_ready(lambda c=controller: api.clear_boot_drive(c["dn"]), log=log,
                             what="clearing the boot drive")
            for vd in vds:
                log(f"deleting virtual drive {vd.get('name')} on {controller.get('id')}")
                _until_ready(lambda v=vd: api.delete_virtual_drive(v["dn"]), log=log,
                             what=f"deleting {vd.get('name')}")
                deleted += 1
    return {"via": "cimc-xml", "deleted": deleted, "description": "every virtual drive deleted"}


def _raid_controller(controllers: list[dict], *, name_of) -> dict:  # noqa: ANN001
    capable = [c for c in controllers if c.get("raid_types")]
    if not capable:
        capable = [c for c in controllers if _RAID.search(name_of(c) or "")
                   and not _NOT_RAID.search(name_of(c) or "")]
    if not capable:
        raise BMCError(
            "no RAID controller exposed; controllers seen: "
            + (", ".join(name_of(c) or "?" for c in controllers) or "none")
        )
    return capable[0]


def _configure_redfish(
    redfish: RedfishDriver, level: RaidLevel, log: LogSink, *, xml_api=None,  # noqa: ANN001
) -> dict:
    controllers = redfish.storage()
    name_of = lambda c: " ".join(filter(None, [c.get("name"), c.get("model")]))  # noqa: E731
    controller = _raid_controller(controllers, name_of=name_of)
    log(f"RAID controller: {controller['name']} ({len(controller['drives'])} drives, "
        f"{len(controller['volumes'])} virtual drives)")
    for d in controller["drives"]:
        log(describe_drive(d))

    # An array that already is what was asked for is kept: rebuilding it
    # gains nothing and the CIMC will not delete its boot drive anyway.
    usable = [d for d in controller["drives"] if unusable_reason(d) is None]
    wanted = len(choose_drives(level, usable)) if len(usable) >= MIN_DRIVES[level] else 0
    kept = next(
        (v for v in controller["volumes"]
         if level_matches(level, v.get("raid_type")) and v.get("drive_count") == wanted
         and str(v.get("health") or "OK").upper() in ("OK",)),
        None,
    ) if len(controller["volumes"]) == 1 else None
    if kept is not None:
        log(f"virtual drive {kept.get('name')} is already {level.value.upper()} over "
            f"{wanted} drives and healthy; keeping it")
        if xml_api is not None:
            _ensure_boot_drive_xml(xml_api, kept.get("name"), log)
        return {
            "via": "redfish", "controller": controller["name"], "level": level.value,
            "drives": [d["name"] for d in usable][:wanted], "volume": kept.get("name"),
            "capacity_bytes": kept.get("capacity_bytes"), "kept": True,
            "description": f"existing {level.value.upper()} virtual drive "
                           f"{kept.get('name')} kept"
                           + (f", {kept['capacity_bytes'] / 1_000_000_000:.0f} GB"
                              if kept.get("capacity_bytes") else ""),
        }

    if controller["volumes"]:
        for volume in controller["volumes"]:
            log(f"deleting virtual drive {volume.get('name')}")
            redfish.delete_volume(volume["path"])
        # Member drives change state once their array is gone.
        controller = next(
            (c for c in redfish.storage() if c["path"] == controller["path"]), controller
        )

    members = choose_drives(level, controller["drives"])
    jbod = [d for d in members if _redfish_drive_is_jbod(d)]
    if jbod:
        # A JBOD disk cannot join an array. Redfish has no verb for the change
        # on this firmware; the XML API does.
        if xml_api is None:
            raise BMCError("drives are in JBOD mode and no XML API is available to change that")
        log(f"{len(jbod)} drive(s) in JBOD mode; making them unconfigured-good over the XML API")
        with xml_api() as api:
            for xml_controller in api.storage_controllers():
                disks = {d.get("id"): d for d in api.local_disks(xml_controller["dn"])}
                for d in jbod:
                    disk_id = str(d.get("id") or "").replace("PD-", "")
                    if disk_id in disks:
                        api.make_unconfigured_good(disks[disk_id]["dn"])
                        log(f"{d['name']}: JBOD -> unconfigured good")
    capacity = usable_capacity(level, [d.get("capacity_bytes") for d in members])
    log(f"building {describe(level, members, capacity)}")
    try:
        redfish.create_volume(
            controller, raid_type=REDFISH_TYPE[level], name=VOLUME_NAME,
            drive_paths=[d["path"] for d in members],
        )
    except BMCError as exc:
        if "CapacityBytes" not in str(exc):
            raise
        # This firmware wants the size spelled out; leave headroom for the
        # controller's own coercion of drive sizes.
        if capacity is None:
            raise BMCError(
                "the CIMC wants CapacityBytes but did not report the drives' sizes"
            ) from exc
        log("the CIMC wants CapacityBytes; retrying with the computed size", level="warning")
        redfish.create_volume(
            controller, raid_type=REDFISH_TYPE[level], name=VOLUME_NAME,
            drive_paths=[d["path"] for d in members],
            capacity_bytes=int(capacity * 0.98) // 1_048_576 * 1_048_576,
        )

    after = next((c for c in redfish.storage() if c["path"] == controller["path"]), None)
    volumes = (after or {}).get("volumes") or []
    built = next(
        (v for v in volumes if v.get("name") == VOLUME_NAME), volumes[-1] if volumes else None
    )
    if built is None:
        raise BMCError("the CIMC accepted the request but lists no virtual drive afterwards")
    log(f"virtual drive {built.get('name')} ({built.get('raid_type')}) is present")
    return {
        "via": "redfish", "controller": controller["name"], "level": level.value,
        "drives": [d["name"] for d in members], "volume": built.get("name"),
        "capacity_bytes": built.get("capacity_bytes") or capacity,
        "description": describe(level, members, built.get("capacity_bytes") or capacity),
    }


def _ensure_boot_drive_xml(xml_api, name: str | None, log: LogSink) -> None:  # noqa: ANN001
    """Mark the named virtual drive bootable over the XML API (Redfish on
    the C220 M4 has no way to). Best effort: logged, never fatal."""
    try:
        with xml_api() as api:
            for controller in api.storage_controllers():
                for vd in api.virtual_drives(controller["dn"]):
                    if vd.get("name") == name and vd.get("bootDrive") != "true":
                        api.set_boot_drive(vd["dn"])
                        log(f"virtual drive {name} marked as the boot drive")
    except BMCError as exc:
        log(f"could not check the boot drive flag ({exc})", level="warning")


def _configure_xml(api: CimcXmlApi, level: RaidLevel, log: LogSink) -> dict:
    controllers = api.storage_controllers()
    controller = _raid_controller(
        controllers,
        name_of=lambda c: " ".join(filter(None, [c.get("id"), c.get("model"), c.get("type")])),
    )
    dn = controller["dn"]
    log(f"RAID controller (XML API): {controller.get('id')} {controller.get('model') or ''}")
    existing = api.virtual_drives(dn)
    if len(existing) == 1:
        vd = existing[0]
        members = int(vd.get("drivesPerSpan") or 0) * int(vd.get("spanDepth") or 1)
        if (level_matches(level, vd.get("raidLevel"))
                and vd.get("vdStatus", "Optimal") == "Optimal"
                and members >= MIN_DRIVES[level]):
            log(f"virtual drive {vd.get('name')} is already {vd.get('raidLevel')} over "
                f"{members} drives and optimal; keeping it")
            if vd.get("bootDrive") != "true":
                api.set_boot_drive(vd["dn"])
                log(f"virtual drive {vd.get('name')} marked as the boot drive")
            return {
                "via": "cimc-xml", "controller": controller.get("id"), "level": level.value,
                "drives": [], "volume": vd.get("name"),
                "capacity_bytes": _parse_size(vd.get("size") or "") or None, "kept": True,
                "description": f"existing {vd.get('raidLevel')} virtual drive "
                               f"{vd.get('name')} kept ({vd.get('size')})",
            }
    if any(vd.get("bootDrive") == "true" for vd in existing):
        # "The Virtual Drive 0 is an OS Drive. This virtual drive cannot be
        # deleted" until the controller's boot drive is cleared.
        log("clearing the controller's boot drive so the virtual drive can be deleted")
        _until_ready(lambda: api.clear_boot_drive(dn), log=log, what="clearing the boot drive")
    for vd in existing:
        log(f"deleting virtual drive {vd.get('name')}")
        try:
            _until_ready(lambda v=vd: api.delete_virtual_drive(v["dn"]), log=log,
                         what=f"deleting {vd.get('name')}")
        except BMCError as exc:
            if "timed out" not in str(exc).lower():
                raise
            log(f"the CIMC did not answer the delete in time ({exc}); checking whether it "
                "happened", level="warning")
            gone = _settle(
                lambda vd_dn=vd["dn"]: all(v["dn"] != vd_dn for v in api.virtual_drives(dn)),
                log=log, what="the virtual drive to disappear",
            )
            if not gone:
                raise BMCError(f"virtual drive {vd.get('name')} is still there after the "
                               "delete timed out") from exc
        log(f"virtual drive {vd.get('name')} deleted")

    def list_disks() -> list[dict[str, str]]:
        found = api.local_disks(dn)
        if not found:
            # The disks' DNs do not hang off the controller's the way imcsdk's
            # do on every build; take every physical disk the CIMC lists.
            found = api.resolve_class("storageLocalDisk")
            log(f"no disks under {dn}; {len(found)} physical disk object(s) in all",
                level="warning")
        return found

    raw_disks = list_disks()
    # Disks that still carry another array's metadata (a C220 M4 whose old
    # array was never deleted shows pdStatus="Foreign Configuration" and
    # health "Moderate Fault") cannot join a new one until the controller
    # clears it.
    if any("foreign" in (d.get("pdStatus") or "").lower() for d in raw_disks):
        log("disks carry a foreign configuration; clearing it on the controller")
        _until_ready(lambda: api.clear_foreign_config(dn), log=log,
                     what="clearing the foreign configuration")
        raw_disks = list_disks()
    disks = []
    for d in raw_disks:
        state = (d.get("pdStatus") or d.get("pdState") or d.get("state")
                 or d.get("operability") or "")
        size = _parse_size(d.get("coercedSize") or d.get("size") or "")
        disk = {
            "id": int(re.sub(r"\D", "", d.get("id") or "") or 0), "dn": d.get("dn", ""),
            "name": f"disk {d.get('id')}", "media": d.get("mediaType"),
            "capacity_bytes": size or None, "state": state or None,
            "health": d.get("health") or None,
        }
        disks.append(disk)
        # What this firmware actually calls things, for the next person.
        shown = {k: v for k, v in d.items()
                 if k in ("id", "pdStatus", "pdState", "health", "coercedSize", "size",
                          "mediaType", "driveFirmware", "predictiveFailureCount",
                          "linkSpeed", "driveState", "operability", "dn")}
        log(f"{describe_drive(disk)}; attributes: {shown}")
    members = choose_drives(level, disks)
    for d in members:
        if "jbod" in str(d["state"]).lower():
            log(f"{d['name']}: JBOD -> unconfigured good")
            _until_ready(lambda disk=d: api.make_unconfigured_good(disk["dn"]), log=log,
                         what=f"{d['name']} to unconfigured good")

    capacity = usable_capacity(level, [d["capacity_bytes"] for d in members])
    if capacity is None:
        raise BMCError("the CIMC did not report the drives' sizes, which the XML API needs")
    ids = [d["id"] for d in members]
    groups = [ids[i:i + 2] for i in range(0, len(ids), 2)] if level is RaidLevel.RAID10 else [ids]
    # coercedSize is what the controller will actually use, so the whole of
    # it can be asked for (a CIMC-built RAID 1 over 952720 MB disks is
    # exactly 952720 MB). A firmware that still objects gets 98%.
    size_mb = capacity // 1_048_576
    log(f"building {describe(level, members, capacity)}")
    def find_built() -> dict | None:
        return next((v for v in api.virtual_drives(dn) if v.get("name") == VOLUME_NAME), None)

    try:
        _until_ready(lambda: api.create_virtual_drive(
            dn, name=VOLUME_NAME, raid_level=XML_LEVEL[level], drive_groups=groups,
            size=f"{size_mb} MB",
        ), log=log, what="creating the virtual drive")
    except BMCError as exc:
        text = str(exc).lower()
        if "timed out" in text:
            log(f"the CIMC did not answer the create in time ({exc}); checking whether it "
                "happened", level="warning")
            if not _settle(lambda: find_built() is not None, log=log,
                           what="the virtual drive to appear"):
                raise BMCError("no virtual drive appeared after the create timed out") from exc
        elif "size" in text:
            log(f"the CIMC rejected {size_mb} MB ({exc}); retrying at 98%", level="warning")
            api.create_virtual_drive(
                dn, name=VOLUME_NAME, raid_level=XML_LEVEL[level], drive_groups=groups,
                size=f"{int(size_mb * 0.98)} MB",
            )
        else:
            raise
    built = find_built()
    if built is None:
        raise BMCError("the CIMC accepted the request but lists no virtual drive afterwards")
    try:
        _until_ready(lambda: api.set_boot_drive(built["dn"]), log=log,
                     what="marking the virtual drive bootable")
    except BMCError as exc:
        log(f"could not mark the virtual drive bootable ({exc})", level="warning")
    return {
        "via": "cimc-xml", "controller": controller.get("id"), "level": level.value,
        "drives": [d["name"] for d in members], "volume": VOLUME_NAME,
        "capacity_bytes": capacity, "description": describe(level, members, capacity),
    }


def _settle(check: Callable[[], bool], *, log: LogSink, what: str, timeout: int = 120) -> bool:
    """Wait for the controller to reflect `what`, polling the CIMC.

    A delete or create that timed out on the wire may well have happened:
    the controller works on, and the CIMC answers when it is done.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return True
        except BMCError as exc:
            log(f"while waiting for {what}: {exc}", level="warning")
        time.sleep(5)
    return False


def _parse_size(text: str) -> int:
    """'952720 MB' / '1.8 TB' -> bytes; 0 when unreadable."""
    match = re.match(r"\s*([\d.]+)\s*([KMGT]?B)", text, re.IGNORECASE)
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2).upper()
    scale = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    return int(value * scale[unit])
