"""Login, session, and API token management."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import client_ip, current_customer
from app.enums import ActorType
from app.models import APIToken, Customer
from app.schemas import (
    APITokenCreate,
    APITokenCreated,
    APITokenOut,
    CustomerOut,
    LoginRequest,
    PasswordChange,
    ProfileUpdate,
    TokenResponse,
)
from app.security import create_access_token, generate_api_token, hash_password, verify_password
from app.services.audit import record_audit

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

#: A bcrypt hash of a value nobody has. Compared against when the account does
#: not exist, so a missing account and a wrong password take the same time.
_DUMMY_HASH = hash_password("not-a-real-password-timing-equaliser")


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> TokenResponse:
    customer = db.execute(
        select(Customer).where(Customer.email == payload.email.lower())
    ).scalar_one_or_none()

    if customer is None:
        verify_password(payload.password, _DUMMY_HASH)
        raise HTTPException(status_code=401, detail="invalid credentials")

    if not verify_password(payload.password, customer.password_hash) or not customer.is_active:
        record_audit(
            db,
            action="auth.login_failed",
            actor_type=ActorType.CUSTOMER,
            actor_id=customer.id,
            actor_label=customer.email,
            source_ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        db.commit()
        raise HTTPException(status_code=401, detail="invalid credentials")

    token, expires_in = create_access_token(customer.id, customer.is_admin)
    record_audit(
        db,
        action="auth.login",
        actor_type=ActorType.ADMIN if customer.is_admin else ActorType.CUSTOMER,
        actor_id=customer.id,
        actor_label=customer.email,
        source_ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    return TokenResponse(
        access_token=token, expires_in=expires_in, is_admin=customer.is_admin
    )


@router.get("/me", response_model=CustomerOut)
def me(customer: Customer = Depends(current_customer)) -> Customer:
    return customer


@router.patch("/me", response_model=CustomerOut)
def update_me(
    payload: ProfileUpdate,
    request: Request,
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> Customer:
    """Contact details and the notification switch. Email is the login and
    the billing key, so it is changed by an admin, not here."""
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(customer, field, value)
    db.add(customer)
    record_audit(
        db,
        action="account.updated",
        actor_type=ActorType.CUSTOMER,
        actor_id=customer.id,
        actor_label=customer.email,
        source_ip=client_ip(request),
        detail={"fields": sorted(changes)},
    )
    db.commit()
    db.refresh(customer)
    return customer


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: PasswordChange,
    request: Request,
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> None:
    """A new password, given the current one. Existing sessions stay valid
    until they expire; API tokens are separate and untouched."""
    if not verify_password(payload.current_password, customer.password_hash):
        record_audit(
            db,
            action="account.password_change_refused",
            actor_type=ActorType.CUSTOMER,
            actor_id=customer.id,
            actor_label=customer.email,
            source_ip=client_ip(request),
        )
        db.commit()
        raise HTTPException(status_code=400, detail="the current password is not right")
    if payload.new_password == payload.current_password:
        raise HTTPException(status_code=400,
                            detail="the new password is the same as the current one")
    customer.password_hash = hash_password(payload.new_password)
    db.add(customer)
    record_audit(
        db,
        action="account.password_changed",
        actor_type=ActorType.CUSTOMER,
        actor_id=customer.id,
        actor_label=customer.email,
        source_ip=client_ip(request),
    )
    db.commit()


@router.get("/tokens", response_model=list[APITokenOut])
def list_tokens(
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> list[APIToken]:
    return list(
        db.execute(
            select(APIToken)
            .where(APIToken.customer_id == customer.id, APIToken.revoked_at.is_(None))
            .order_by(APIToken.created_at.desc())
        )
        .scalars()
        .all()
    )


@router.post("/tokens", response_model=APITokenCreated, status_code=status.HTTP_201_CREATED)
def create_token(
    payload: APITokenCreate,
    request: Request,
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> APITokenCreated:
    raw_token, prefix, token_hash = generate_api_token()
    record = APIToken(
        customer_id=customer.id,
        name=payload.name,
        token_prefix=prefix,
        token_hash=token_hash,
        expires_at=(
            datetime.now(UTC) + timedelta(days=payload.expires_in_days)
            if payload.expires_in_days
            else None
        ),
    )
    db.add(record)
    record_audit(
        db,
        action="auth.token_created",
        actor_type=ActorType.CUSTOMER,
        actor_id=customer.id,
        actor_label=customer.email,
        target_type="api_token",
        source_ip=client_ip(request),
        detail={"name": payload.name},
    )
    db.commit()
    db.refresh(record)
    # The only time the plaintext exists outside the caller's memory.
    return APITokenCreated(**APITokenOut.model_validate(record).model_dump(), token=raw_token)


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_token(
    token_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> None:
    record = db.get(APIToken, token_id)
    if record is None or record.customer_id != customer.id:
        raise HTTPException(status_code=404, detail="token not found")
    record.revoked_at = datetime.now(UTC)
    db.add(record)
    record_audit(
        db,
        action="auth.token_revoked",
        actor_type=ActorType.CUSTOMER,
        actor_id=customer.id,
        actor_label=customer.email,
        target_type="api_token",
        target_id=str(token_id),
        source_ip=client_ip(request),
    )
    db.commit()
