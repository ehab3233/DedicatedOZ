"""Customer KVM access: a grant per server, and the check nginx makes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import client_ip, current_customer, get_owned_server
from app.enums import ActorType
from app.models import Customer, Server
from app.services import kvm_access
from app.services.audit import record_audit

router = APIRouter(prefix="/api/v1", tags=["kvm"])


def _describe(record, *, password: str | None = None) -> dict:  # noqa: ANN001
    return {
        "url": kvm_access.url(record),
        "hostname": kvm_access.hostname(record),
        "username": record.cimc_username,
        "password": password,
        "expires_at": record.expires_at.isoformat(),
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "hours": settings.kvm_grant_hours,
    }


@router.get("/servers/{server_id}/kvm")
def kvm_status(
    server: Server = Depends(get_owned_server),
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> dict:
    """Whether the graphical console is available here, and the customer's
    open grant on this server if there is one (never the password)."""
    if not kvm_access.enabled():
        return {"available": False, "grant": None,
                "reason": "the graphical console is not set up on this platform"}
    record = kvm_access.active_grant(db, server, customer)
    return {"available": True, "grant": _describe(record) if record else None, "reason": None}


@router.post("/servers/{server_id}/kvm", status_code=status.HTTP_201_CREATED)
def open_kvm(
    request: Request,
    response: Response,
    server: Server = Depends(get_owned_server),
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> dict:
    """Open the graphical console for a few hours: a temporary CIMC login
    (shown once) and a hostname that proxies to the server's BMC for this
    browser. Calling it again gives a fresh password and a fresh expiry."""
    try:
        record, password, ticket = kvm_access.grant(db, server, customer)
    except kvm_access.KvmAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit(
        db,
        action="kvm.granted",
        actor_type=ActorType.CUSTOMER,
        actor_id=customer.id,
        actor_label=customer.email,
        target_type="server",
        target_id=str(server.id),
        source_ip=client_ip(request),
        detail={"hostname": kvm_access.hostname(record), "cimc_user": record.cimc_username,
                "expires_at": record.expires_at.isoformat()},
    )
    db.commit()
    db.refresh(record)
    response.set_cookie(
        kvm_access.COOKIE, ticket, max_age=settings.kvm_grant_hours * 3600,
        domain=kvm_access.cookie_domain(), path="/", secure=True, httponly=True,
        samesite="lax",
    )
    return _describe(record, password=password)


@router.delete("/servers/{server_id}/kvm", status_code=status.HTTP_204_NO_CONTENT)
def close_kvm(
    request: Request,
    server: Server = Depends(get_owned_server),
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> None:
    record = kvm_access.active_grant(db, server, customer)
    if record is None:
        return
    removed = kvm_access.revoke(db, record)
    record_audit(
        db,
        action="kvm.revoked",
        actor_type=ActorType.CUSTOMER,
        actor_id=customer.id,
        actor_label=customer.email,
        target_type="server",
        target_id=str(server.id),
        source_ip=client_ip(request),
        detail={"cimc_user": record.cimc_username, "cimc_user_removed": removed},
    )
    db.commit()


@router.get("/kvm/authorize", include_in_schema=False)
def authorize(
    response: Response,
    db: Session = Depends(get_db),
    x_kvm_host: str | None = Header(default=None),
    host: str | None = Header(default=None),
    cookie: str | None = Header(default=None),
) -> Response:
    """nginx's auth_request for kvm-*.<domain>: 204 with the CIMC to proxy
    to in X-Upstream, or 401. No session header here: the browser is on the
    KVM hostname, so it is the ticket cookie that identifies the customer."""
    ticket = None
    for part in (cookie or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == kvm_access.COOKIE:
            ticket = value
    target = kvm_access.authorize(db, (x_kvm_host or host or "").lower(), ticket)
    if target is None:
        raise HTTPException(status_code=401, detail="no console access for this address")
    db.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    response.headers["X-Upstream"] = target["upstream"]
    response.headers["X-Upstream-Host"] = target["host"]
    return response
