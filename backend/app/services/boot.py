"""Netboot script and answer-file rendering.

Two things are produced here:

* the iPXE script a machine fetches after chaining to the control plane, and
* the OS answer file (kickstart / autoinstall / preseed / unattend) plus the
  provisioning shell script the ramdisk runs.

Both are rendered per MAC from the job payload, so a server's boot script is
only meaningful while it has an active provisioning job.
"""

from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.enums import JobState, JobType
from app.models import IPAssignment, Job, OSTemplate, Server, SSHKey, Subscription
from app.security import boot_signature


def template_dir() -> Path:
    """Locate installer templates in-repo or in the container image."""
    override = os.environ.get("DOZ_INSTALLER_TEMPLATE_DIR")
    if override:
        return Path(override)
    candidates = [
        Path("/opt/doz/installer/templates"),
        Path(__file__).resolve().parents[3] / "installer" / "templates",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise RuntimeError(
        "installer templates not found; set DOZ_INSTALLER_TEMPLATE_DIR"
    )


@lru_cache
def jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(template_dir())),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        # Boot scripts and answer files are not HTML; escaping them would
        # corrupt shell quoting. Autoescape stays off except for markup.
        autoescape=select_autoescape(enabled_extensions=("html", "xml")),
    )


class NoActiveInstall(LookupError):
    pass


def find_provisioning_job(db: Session, mac: str) -> tuple[Server, Job]:
    """Resolve a MAC to the server and its in-flight install/rescue/wipe job."""
    server = db.execute(
        select(Server).where(Server.provisioning_mac == mac)
    ).scalar_one_or_none()
    if server is None:
        raise NoActiveInstall(f"no server registered with provisioning MAC {mac}")

    job = db.execute(
        select(Job)
        .where(
            Job.server_id == server.id,
            Job.type.in_([JobType.INSTALL.value, JobType.RESCUE.value, JobType.WIPE.value]),
            Job.state.in_([JobState.QUEUED.value, JobState.RUNNING.value]),
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if job is None:
        raise NoActiveInstall(f"server {server.serial} has no active provisioning job")
    return server, job


def build_network_context(db: Session, server: Server) -> dict:
    """Primary address, prefix, gateway and DNS for the installed OS.

    Static addressing is used rather than DHCP: the customer plane has no DHCP
    server by design, and an address that changes under a customer is a support
    ticket waiting to happen.
    """
    assignment = db.execute(
        select(IPAssignment)
        .where(
            IPAssignment.server_id == server.id,
            IPAssignment.released_at.is_(None),
            IPAssignment.is_primary.is_(True),
        )
        .limit(1)
    ).scalar_one_or_none()

    if assignment is None:
        return {"configured": False}

    block = assignment.block
    address = str(assignment.address)
    prefix_len = assignment.prefix_len
    network = ipaddress.ip_network(f"{address}/{prefix_len}", strict=False)

    return {
        "configured": True,
        "address": address,
        "prefix_len": prefix_len,
        "netmask": str(network.netmask) if network.version == 4 else None,
        "cidr": f"{address}/{prefix_len}",
        "gateway": str(block.gateway) if block and block.gateway else None,
        "vlan": server.customer_vlan,
        "dns": ["1.1.1.1", "8.8.8.8"],
    }


def collect_ssh_keys(db: Session, server: Server) -> list[str]:
    """Public keys belonging to the customer currently holding the server."""
    subscription = db.execute(
        select(Subscription)
        .where(Subscription.server_id == server.id, Subscription.ended_at.is_(None))
        .limit(1)
    ).scalar_one_or_none()
    if subscription is None:
        return []
    keys = (
        db.execute(select(SSHKey).where(SSHKey.customer_id == subscription.customer_id))
        .scalars()
        .all()
    )
    return [k.public_key.strip() for k in keys]


def install_context(db: Session, server: Server, job: Job) -> dict:
    """Everything both the iPXE script and the answer file need."""
    payload = job.payload or {}
    template: OSTemplate | None = None
    if payload.get("os_template_id"):
        template = db.get(OSTemplate, payload["os_template_id"])

    mac = server.provisioning_mac or ""
    return {
        "server": {
            "id": str(server.id),
            "serial": server.serial,
            "hostname": payload.get("hostname") or server.hostname or f"srv-{server.serial}",
            "mac": mac,
            "model": server.model,
        },
        "job": {
            "id": str(job.id),
            "type": job.type,
            # Populated by the caller that owns the plaintext token.
            "callback_token": payload.get("_callback_token", ""),
        },
        "os": (
            {
                "slug": template.slug,
                "name": template.name,
                "family": template.family,
                "version": template.version,
                "install_method": template.install_method,
                "kernel_url": _asset_url(template.kernel_path),
                "initrd_url": _asset_url(template.initrd_path),
                "iso_url": _asset_url(template.iso_path),
                "kernel_args": template.kernel_args or "",
            }
            if template
            else None
        ),
        "raid": {
            "level": payload.get("raid_level", "raid1"),
            "wipe": payload.get("wipe_disks", True),
        },
        "network": build_network_context(db, server),
        "ssh_keys": payload.get("ssh_keys") or collect_ssh_keys(db, server),
        "root_password_hash": payload.get("root_password_hash"),
        "control_plane_url": settings.control_plane_url.rstrip("/"),
        "boot_asset_base_url": settings.boot_asset_base_url.rstrip("/"),
        "boot_signature": boot_signature(mac) if mac else "",
    }


def _asset_url(path: str | None) -> str | None:
    if not path:
        return None
    if path.startswith(("http://", "https://", "tftp://")):
        return path
    return f"{settings.boot_asset_base_url.rstrip('/')}/{path.lstrip('/')}"


def render(name: str, context: dict) -> str:
    return jinja_env().get_template(name).render(**context)


def render_ipxe_script(db: Session, server: Server, job: Job) -> str:
    """Pick the right iPXE script for the job type."""
    context = install_context(db, server, job)
    template_name = {
        JobType.INSTALL.value: "ipxe/install.ipxe.j2",
        JobType.RESCUE.value: "ipxe/rescue.ipxe.j2",
        JobType.WIPE.value: "ipxe/wipe.ipxe.j2",
    }[job.type]
    return render(template_name, context)


def render_answer_file(db: Session, server: Server, job: Job) -> tuple[str, str]:
    """Return `(content_type, body)` for the OS answer file."""
    context = install_context(db, server, job)
    os_ctx = context["os"]
    if not os_ctx:
        raise NoActiveInstall("job has no OS template attached")

    template_id = (job.payload or {}).get("os_template_id")
    template = db.get(OSTemplate, template_id)
    body = render(f"answers/{template.config_template}", context)

    content_type = {
        "autoinstall": "text/yaml",
        "unattend": "application/xml",
    }.get(template.install_method, "text/plain")
    return content_type, body


def render_provision_script(db: Session, server: Server, job: Job) -> str:
    """The shell script the rescue ramdisk executes to wipe/RAID/install."""
    context = install_context(db, server, job)
    return render("ramdisk/provision.sh.j2", context)
