"""Customer access to a server's graphical console.

The CIMC's HTML5 viewer must be opened against the CIMC itself, which sits
on a network customers cannot see, and on this firmware it needs a CIMC
login rather than a launch token. So a grant does two things:

1. creates a temporary CIMC user of role "user" (power, KVM, virtual media;
   no BMC settings) for the length of the grant, and
2. opens a hostname `kvm-<label>.<portal domain>` that nginx proxies to that
   CIMC, for a browser carrying the customer's signed ticket (a cookie set
   by the portal on the portal's domain).

When the grant ends, by time or by hand, the CIMC user is removed and the
hostname stops answering. Every step is audited.
"""

from __future__ import annotations

import secrets
import string
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.drivers.base import BMCError, LogSink, null_sink
from app.drivers.cimc import CimcXmlApi
from app.drivers.factory import credential_for
from app.models import Customer, KvmGrant, Server
from app.secrets import SecretNotFoundError
from app.security import create_kvm_ticket, decode_kvm_ticket

COOKIE = "doz_kvm"
_ALPHABET = string.ascii_letters + string.digits


class KvmAccessError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(settings.portal_domain)


def hostname(grant: KvmGrant) -> str:
    return f"{grant.label}.{settings.portal_domain}"


def url(grant: KvmGrant) -> str:
    return f"https://{hostname(grant)}/"


def cookie_domain() -> str:
    """The cookie must reach kvm-*.<domain>: scope it to the domain itself."""
    return f".{settings.portal_domain}"


def _password() -> str:
    """16 characters the CIMC accepts: letters and digits, both cases, a digit."""
    while True:
        candidate = "".join(secrets.choice(_ALPHABET) for _ in range(16))
        if (any(c.islower() for c in candidate) and any(c.isupper() for c in candidate)
                and any(c.isdigit() for c in candidate)):
            return candidate


def _api(server: Server, log: LogSink) -> CimcXmlApi:
    return CimcXmlApi(str(server.cimc_ip), credential_for(server), port=server.redfish_port,
                      timeout=20, log=log)


def active_grant(db: Session, server: Server, customer: Customer) -> KvmGrant | None:
    now = datetime.now(UTC)
    return db.execute(
        select(KvmGrant).where(
            KvmGrant.server_id == server.id,
            KvmGrant.customer_id == customer.id,
            KvmGrant.revoked_at.is_(None),
            KvmGrant.expires_at > now,
        ).order_by(KvmGrant.created_at.desc())
    ).scalars().first()


def grant(db: Session, server: Server, customer: Customer, *,
          log: LogSink = null_sink) -> tuple[KvmGrant, str, str]:
    """Open (or renew) the customer's window onto `server`.

    Returns the grant, the CIMC password (shown once; never stored) and the
    ticket for the cookie. A grant already open gets a fresh password and a
    fresh expiry rather than a second CIMC user.
    """
    if not enabled():
        raise KvmAccessError("the graphical console is not set up on this platform "
                             "(no portal domain)")
    hours = settings.kvm_grant_hours
    password = _password()
    existing = active_grant(db, server, customer)
    try:
        with _api(server, log) as api:
            if existing is not None:
                try:
                    api.set_user_password(existing.cimc_username, password)
                except BMCError:
                    # The slot was taken away under us; start again.
                    api.create_user(existing.cimc_username, password)
                record = existing
            else:
                record = KvmGrant(
                    server_id=server.id, customer_id=customer.id,
                    label=f"kvm-{secrets.token_hex(5)}",
                    cimc_username=f"doz{secrets.token_hex(4)}",
                    expires_at=datetime.now(UTC) + timedelta(hours=hours),
                )
                api.create_user(record.cimc_username, password)
    except (BMCError, SecretNotFoundError, ValueError) as exc:
        raise KvmAccessError(f"the server's BMC did not accept a console user: {exc}") from exc
    record.expires_at = datetime.now(UTC) + timedelta(hours=hours)
    record.revoked_at = None
    record.cleaned_at = None
    db.add(record)
    db.flush()
    return record, password, create_kvm_ticket(customer.id, hours)


def revoke(db: Session, record: KvmGrant, *, log: LogSink = null_sink) -> bool:
    """Close the window: remove the CIMC user, mark the grant. True when the
    CIMC user was removed (False when the BMC could not be reached; the
    expiry sweep tries again)."""
    now = datetime.now(UTC)
    record.revoked_at = record.revoked_at or now
    removed = False
    server = db.get(Server, record.server_id)
    if server is not None:
        try:
            with _api(server, log) as api:
                api.remove_user(record.cimc_username)
            removed = True
        except (BMCError, SecretNotFoundError, ValueError) as exc:
            log(f"could not remove CIMC user {record.cimc_username}: {exc}", level="warning")
    if removed:
        record.cleaned_at = now
    db.add(record)
    return removed


def expire_due(db: Session, *, log: LogSink = null_sink) -> int:
    """Remove the CIMC users of grants that have run out. Returns how many."""
    now = datetime.now(UTC)
    due = db.execute(
        select(KvmGrant).where(
            KvmGrant.cleaned_at.is_(None),
            (KvmGrant.expires_at <= now) | (KvmGrant.revoked_at.is_not(None)),
        )
    ).scalars().all()
    cleaned = 0
    for record in due:
        if revoke(db, record, log=log):
            cleaned += 1
    return cleaned


def authorize(db: Session, host: str, ticket: str | None) -> dict[str, str] | None:
    """What nginx asks before proxying a request to `host`: is this browser
    allowed through, and to which CIMC? None means no."""
    if not enabled() or not ticket:
        return None
    suffix = f".{settings.portal_domain}"
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    try:
        payload = decode_kvm_ticket(ticket)
        customer_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
    now = datetime.now(UTC)
    record = db.execute(
        select(KvmGrant).where(
            KvmGrant.label == label,
            KvmGrant.customer_id == customer_id,
            KvmGrant.revoked_at.is_(None),
            KvmGrant.expires_at > now,
        )
    ).scalar_one_or_none()
    if record is None:
        return None
    server = db.get(Server, record.server_id)
    if server is None:
        return None
    record.last_seen_at = now
    db.add(record)
    port = f":{server.redfish_port}" if server.redfish_port else ""
    return {"upstream": f"https://{server.cimc_ip}{port}", "host": f"{server.cimc_ip}{port}"}
