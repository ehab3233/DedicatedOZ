"""Job construction for the provisioning rails.

Install, rescue and wipe are the same netboot rail pointed at different
images, so they share their setup here. Building the job row is separated from
dispatching it: the callback token has to be embedded in the payload before
the worker can pick the job up, and both must land in one transaction.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import ActorType, JobType, RaidLevel, ServerState
from app.models import Customer, Job, OSTemplate, Server, SSHKey
from app.security import hash_password
from app.services import jobs as job_service


class ProvisioningError(ValueError):
    pass


#: States a server may be reinstalled from.
REINSTALLABLE = {ServerState.ACTIVE, ServerState.RESCUE, ServerState.IN_STOCK}


def _resolve_ssh_keys(
    db: Session, customer: Customer | None, key_ids: list[uuid.UUID]
) -> list[str]:
    """Explicit key selection, or every key the customer owns.

    An install with no keys and no root password is unreachable, so the caller
    is expected to check that combination before getting here.
    """
    if customer is None:
        return []
    query = select(SSHKey).where(SSHKey.customer_id == customer.id)
    if key_ids:
        query = query.where(SSHKey.id.in_(key_ids))
    keys = db.execute(query).scalars().all()

    if key_ids and len(keys) != len(set(key_ids)):
        raise ProvisioningError("one or more SSH keys do not exist or are not yours")
    return [k.public_key.strip() for k in keys]


def create_install_job(
    db: Session,
    server: Server,
    *,
    os_template_id: uuid.UUID,
    hostname: str | None,
    raid_level: RaidLevel,
    ssh_key_ids: list[uuid.UUID],
    root_password: str | None,
    customer: Customer | None,
    actor_type: ActorType = ActorType.CUSTOMER,
) -> Job:
    if ServerState(server.state) not in REINSTALLABLE:
        raise ProvisioningError(
            f"server is {server.state}; reinstall is only possible from "
            + ", ".join(sorted(s.value for s in REINSTALLABLE))
        )
    if not server.provisioning_mac:
        raise ProvisioningError(
            "server has no provisioning MAC recorded; run an inventory sync first"
        )

    template = db.get(OSTemplate, os_template_id)
    if template is None or not template.is_enabled:
        raise ProvisioningError("unknown OS template")
    if not template.is_public and actor_type is ActorType.CUSTOMER:
        raise ProvisioningError("unknown OS template")

    ssh_keys = _resolve_ssh_keys(db, customer, ssh_key_ids)
    if not ssh_keys and not root_password:
        raise ProvisioningError(
            "provide at least one SSH key or a root password, or the server "
            "will be unreachable after install"
        )

    payload = {
        "os_template_id": str(os_template_id),
        "os_slug": template.slug,
        "hostname": hostname or server.hostname or f"srv-{server.serial}",
        "raid_level": raid_level.value,
        "ssh_keys": ssh_keys,
        "wipe_disks": True,
    }
    if root_password:
        # Only the hash travels: it goes into the answer file, and the
        # plaintext must not be recoverable from the job row afterwards.
        payload["root_password_hash"] = hash_password(root_password)

    job, token = job_service.create_job(
        db,
        job_type=JobType.INSTALL,
        server_id=server.id,
        payload=payload,
        requested_by_id=customer.id if customer else None,
        requested_by_type=actor_type,
        with_callback_token=True,
    )
    _attach_callback_token(db, job, token)
    return job


def create_rescue_job(
    db: Session,
    server: Server,
    *,
    ssh_key_ids: list[uuid.UUID],
    customer: Customer | None,
    actor_type: ActorType = ActorType.CUSTOMER,
) -> Job:
    if not server.provisioning_mac:
        raise ProvisioningError(
            "server has no provisioning MAC recorded; run an inventory sync first"
        )
    ssh_keys = _resolve_ssh_keys(db, customer, ssh_key_ids)
    if not ssh_keys:
        raise ProvisioningError("rescue mode needs at least one SSH key to log in with")

    job, token = job_service.create_job(
        db,
        job_type=JobType.RESCUE,
        server_id=server.id,
        payload={"ssh_keys": ssh_keys, "wipe_disks": False},
        requested_by_id=customer.id if customer else None,
        requested_by_type=actor_type,
        with_callback_token=True,
    )
    _attach_callback_token(db, job, token)
    return job


def create_wipe_job(
    db: Session,
    server: Server,
    *,
    method: str,
    requested_by: Customer | None,
    actor_type: ActorType = ActorType.ADMIN,
) -> Job:
    if method not in {"secure", "zero"}:
        raise ProvisioningError("wipe method must be 'secure' or 'zero'")
    if not server.provisioning_mac:
        raise ProvisioningError(
            "server has no provisioning MAC recorded; run an inventory sync first"
        )

    job, token = job_service.create_job(
        db,
        job_type=JobType.WIPE,
        server_id=server.id,
        payload={"wipe_method": method, "wipe_disks": True},
        requested_by_id=requested_by.id if requested_by else None,
        requested_by_type=actor_type,
        with_callback_token=True,
    )
    _attach_callback_token(db, job, token)
    return job


def _attach_callback_token(db: Session, job: Job, token: str | None) -> None:
    """Stash the plaintext callback token in the payload.

    It has to be readable when the boot script is rendered, minutes later and
    in a different process. It is cleared the moment the job reaches a terminal
    state, and the payload is admin-only in the API.
    """
    if token:
        payload = dict(job.payload or {})
        payload["_callback_token"] = token
        job.payload = payload
        db.add(job)
