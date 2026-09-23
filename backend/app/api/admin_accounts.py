"""Admin API: customers, subscriptions and BMC credentials.

Split from `admin.py` because it is about *who* holds a server rather than
the server itself. Handing a server to a customer is the moment it stops
being inventory, so it is audited like a lifecycle change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import client_ip, current_admin
from app.enums import ActorType, ServerState
from app.models import Customer, IPAssignment, Server, Subscription
from app.schemas import (
    AdminCustomerOut,
    CredentialCreate,
    CustomerCreate,
    CustomerUpdate,
    SubscriptionCreate,
    SubscriptionOut,
)
from app.secrets import BMCCredential, SecretNotFoundError, get_secrets_backend
from app.security import hash_password
from app.services.audit import record_audit

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(current_admin)])


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@router.post("/credentials", status_code=status.HTTP_201_CREATED)
def store_credential(
    payload: CredentialCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> dict:
    """Store a CIMC credential under a ref, for backends that support writes.

    The env backend is read-only by design; the panel tells the operator to
    set the variable instead. The password is never logged.
    """
    backend = get_secrets_backend()
    try:
        backend.put_bmc_credential(
            payload.ref, BMCCredential(username=payload.username, password=payload.password)
        )
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "the configured secrets backend is read-only; set "
                "DOZ_CIMC_DEFAULT_USER/PASS or a per-ref variable instead"
            ),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    record_audit(
        db,
        action="credential.stored",
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="credential",
        target_id=payload.ref,
        source_ip=client_ip(request),
    )
    db.commit()
    return {"ref": payload.ref, "stored": True}


@router.get("/credentials/check")
def check_credential(ref: str) -> dict:
    """Does this ref resolve? Never returns the secret itself."""
    try:
        cred = get_secrets_backend().get_bmc_credential(ref)
    except (SecretNotFoundError, ValueError):
        return {"ref": ref, "resolves": False}
    return {"ref": ref, "resolves": True, "username": cred.username}


@router.get("/credentials/backend")
def credential_backend() -> dict:
    from app.config import settings

    backend = settings.secrets_backend.lower()
    return {"backend": backend, "writable": backend in {"file", "vault"}}


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


@router.get("/customers", response_model=list[AdminCustomerOut])
def list_customers(db: Session = Depends(get_db)) -> list[AdminCustomerOut]:
    customers = db.execute(select(Customer).order_by(Customer.email)).scalars().all()
    counts = dict(
        db.execute(
            select(Subscription.customer_id, func.count())
            .where(Subscription.ended_at.is_(None))
            .group_by(Subscription.customer_id)
        ).all()
    )
    out = []
    for c in customers:
        item = AdminCustomerOut.model_validate(c)
        item.active_servers = counts.get(c.id, 0)
        out.append(item)
    return out


@router.post("/customers", response_model=AdminCustomerOut, status_code=201)
def create_customer(
    payload: CustomerCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> AdminCustomerOut:
    email = payload.email.lower()
    if db.execute(select(Customer).where(Customer.email == email)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="email already registered")
    customer = Customer(
        email=email,
        password_hash=hash_password(payload.password),
        company_name=payload.company_name,
        contact_name=payload.contact_name,
        phone=payload.phone,
        billing_ref=payload.billing_ref,
        is_admin=payload.is_admin,
    )
    db.add(customer)
    record_audit(
        db,
        action="customer.created",
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="customer",
        target_id=email,
        source_ip=client_ip(request),
        detail={"is_admin": payload.is_admin},
    )
    db.commit()
    db.refresh(customer)
    return AdminCustomerOut.model_validate(customer)


@router.get("/customers/{customer_id}", response_model=AdminCustomerOut)
def get_customer(customer_id: uuid.UUID, db: Session = Depends(get_db)) -> AdminCustomerOut:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="customer not found")
    item = AdminCustomerOut.model_validate(customer)
    item.active_servers = db.execute(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.customer_id == customer_id, Subscription.ended_at.is_(None))
    ).scalar_one()
    return item


@router.patch("/customers/{customer_id}", response_model=AdminCustomerOut)
def update_customer(
    customer_id: uuid.UUID,
    payload: CustomerUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> AdminCustomerOut:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="customer not found")
    changes = payload.model_dump(exclude_unset=True)
    password = changes.pop("password", None)
    for field, value in changes.items():
        setattr(customer, field, value)
    if password:
        customer.password_hash = hash_password(password)
    db.add(customer)
    record_audit(
        db,
        action="customer.updated",
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="customer",
        target_id=str(customer_id),
        source_ip=client_ip(request),
        detail={"fields": sorted(changes) + (["password"] if password else [])},
    )
    db.commit()
    return get_customer(customer_id, db)


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


@router.get("/subscriptions", response_model=list[SubscriptionOut])
def list_subscriptions(
    customer_id: uuid.UUID | None = None,
    server_id: uuid.UUID | None = None,
    include_ended: bool = False,
    db: Session = Depends(get_db),
) -> list[SubscriptionOut]:
    query = select(Subscription).order_by(Subscription.started_at.desc())
    if customer_id:
        query = query.where(Subscription.customer_id == customer_id)
    if server_id:
        query = query.where(Subscription.server_id == server_id)
    if not include_ended:
        query = query.where(Subscription.ended_at.is_(None))
    return [_subscription_out(db, sub) for sub in db.execute(query).scalars().all()]


@router.post("/subscriptions", response_model=SubscriptionOut, status_code=201)
def create_subscription(
    payload: SubscriptionCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> SubscriptionOut:
    """Hand a server to a customer.

    Existing IP assignments on the server are re-attributed to the new
    customer so the history behind a later abuse report stays correct.
    """
    customer = db.get(Customer, payload.customer_id)
    server = db.get(Server, payload.server_id)
    if customer is None or server is None:
        raise HTTPException(status_code=404, detail="customer or server not found")
    if ServerState(server.state) not in {ServerState.IN_STOCK, ServerState.ACTIVE}:
        raise HTTPException(
            status_code=409, detail=f"server is {server.state}; it cannot be assigned"
        )
    existing = db.execute(
        select(Subscription).where(
            Subscription.server_id == server.id, Subscription.ended_at.is_(None)
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="server already has an active subscription")

    sub = Subscription(**payload.model_dump())
    db.add(sub)
    for assignment in (
        db.execute(
            select(IPAssignment).where(
                IPAssignment.server_id == server.id, IPAssignment.released_at.is_(None)
            )
        )
        .scalars()
        .all()
    ):
        assignment.customer_id = customer.id
        db.add(assignment)

    record_audit(
        db,
        action="subscription.created",
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="server",
        target_id=str(server.id),
        source_ip=client_ip(request),
        detail={"customer": customer.email, "plan": payload.plan_name},
    )
    db.commit()
    db.refresh(sub)
    return _subscription_out(db, sub)


@router.post("/subscriptions/{subscription_id}/end", response_model=SubscriptionOut)
def end_subscription(
    subscription_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> SubscriptionOut:
    """End a subscription. Does not wipe the server — queue that next, and do."""
    sub = db.get(Subscription, subscription_id)
    if sub is None or sub.ended_at is not None:
        raise HTTPException(status_code=404, detail="active subscription not found")
    sub.ended_at = datetime.now(UTC)
    db.add(sub)
    record_audit(
        db,
        action="subscription.ended",
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="server",
        target_id=str(sub.server_id),
        source_ip=client_ip(request),
    )
    db.commit()
    db.refresh(sub)
    return _subscription_out(db, sub)


def _subscription_out(db: Session, sub: Subscription) -> SubscriptionOut:
    out = SubscriptionOut.model_validate(sub)
    customer = db.get(Customer, sub.customer_id)
    server = db.get(Server, sub.server_id)
    out.customer_email = customer.email if customer else None
    out.server_serial = server.serial if server else None
    return out
