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

from app.drivers.base import BMCError, LogSink, null_sink
from app.drivers.cimc import CimcXmlApi
from app.drivers.factory import credential_for
from app.drivers.redfish import RedfishDriver
from app.enums import RaidLevel
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


def _redfish_drive_is_jbod(d: dict) -> bool:
    return "jbod" in str(d.get("oem_state") or d.get("state") or "").lower()


def describe(level: RaidLevel, drives: list[dict], capacity_bytes: int | None) -> str:
    names = ", ".join(str(d.get("name") or d.get("id")) for d in drives)
    size = f", {capacity_bytes / 1_000_000_000:.0f} GB" if capacity_bytes else ""
    return f"{level.value.upper()} over {len(drives)} drives ({names}){size}"


def configure(server: Server, level: RaidLevel, *, log: LogSink = null_sink) -> dict:
    """Replace whatever the controller holds with one `level` virtual drive."""
    credential = credential_for(server)
    host = str(server.cimc_ip)
    errors: list[str] = []
    redfish = RedfishDriver(
        host=host, credential=credential, port=server.redfish_port,
        system_path=server.redfish_system_path, log=log,
    )

    def xml_api() -> CimcXmlApi:
        return CimcXmlApi(host, credential, port=server.redfish_port, log=log)

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
    with CimcXmlApi(host, credential, port=server.redfish_port, log=log) as api:
        deleted = 0
        for controller in api.storage_controllers():
            for vd in api.virtual_drives(controller["dn"]):
                log(f"deleting virtual drive {vd.get('name')} on {controller.get('id')}")
                api.delete_virtual_drive(vd["dn"])
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


def _configure_xml(api: CimcXmlApi, level: RaidLevel, log: LogSink) -> dict:
    controllers = api.storage_controllers()
    controller = _raid_controller(
        controllers,
        name_of=lambda c: " ".join(filter(None, [c.get("id"), c.get("model"), c.get("type")])),
    )
    dn = controller["dn"]
    log(f"RAID controller (XML API): {controller.get('id')} {controller.get('model') or ''}")
    for vd in api.virtual_drives(dn):
        log(f"deleting virtual drive {vd.get('name')}")
        api.delete_virtual_drive(vd["dn"])

    raw_disks = api.local_disks(dn)
    if not raw_disks:
        # The disks' DNs do not hang off the controller's the way imcsdk's
        # do on every build; take every physical disk the CIMC lists.
        raw_disks = api.resolve_class("storageLocalDisk")
        log(f"no disks under {dn}; {len(raw_disks)} physical disk object(s) in all",
            level="warning")
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
            api.make_unconfigured_good(d["dn"])

    capacity = usable_capacity(level, [d["capacity_bytes"] for d in members])
    if capacity is None:
        raise BMCError("the CIMC did not report the drives' sizes, which the XML API needs")
    ids = [d["id"] for d in members]
    groups = [ids[i:i + 2] for i in range(0, len(ids), 2)] if level is RaidLevel.RAID10 else [ids]
    size_mb = int(capacity * 0.98) // 1_048_576
    log(f"building {describe(level, members, capacity)}")
    api.create_virtual_drive(
        dn, name=VOLUME_NAME, raid_level=XML_LEVEL[level], drive_groups=groups,
        size=f"{size_mb} MB",
    )
    built = next((v for v in api.virtual_drives(dn) if v.get("name") == VOLUME_NAME), None)
    if built is None:
        raise BMCError("the CIMC accepted the request but lists no virtual drive afterwards")
    try:
        api.set_boot_drive(built["dn"])
    except BMCError as exc:
        log(f"could not mark the virtual drive bootable ({exc})", level="warning")
    return {
        "via": "cimc-xml", "controller": controller.get("id"), "level": level.value,
        "drives": [d["name"] for d in members], "volume": VOLUME_NAME,
        "capacity_bytes": capacity, "description": describe(level, members, capacity),
    }


def _parse_size(text: str) -> int:
    """'952720 MB' / '1.8 TB' -> bytes; 0 when unreadable."""
    match = re.match(r"\s*([\d.]+)\s*([KMGT]?B)", text, re.IGNORECASE)
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2).upper()
    scale = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    return int(value * scale[unit])
