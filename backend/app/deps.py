"""FastAPI dependencies: authentication and object lookup with ownership checks."""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import APIToken, Customer, Job, Server, Subscription
from app.security import decode_access_token, hash_api_token, token_prefix

UNAUTHORISED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def current_customer(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> Customer:
    """Resolve a bearer credential to a customer.

    Accepts both session JWTs and long-lived API tokens on the same header, so
    the public API and the portal share one authorisation path — there is no
    second, weaker way in.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UNAUTHORISED
    token = authorization[7:].strip()

    customer = (
        _customer_from_api_token(db, token)
        if token.startswith("doz_")
        else _customer_from_jwt(db, token)
    )
    if customer is None or not customer.is_active:
        raise UNAUTHORISED
    return customer


def _customer_from_jwt(db: Session, token: str) -> Customer | None:
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        return None
    try:
        customer_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        return None
    return db.get(Customer, customer_id)


def _customer_from_api_token(db: Session, token: str) -> Customer | None:
    # The prefix is indexed; the hash is what actually authenticates.
    record = db.execute(
        select(APIToken).where(APIToken.token_prefix == token_prefix(token))
    ).scalar_one_or_none()
    if record is None or record.token_hash != hash_api_token(token):
        return None

    now = datetime.now(UTC)
    if record.revoked_at is not None:
        return None
    if record.expires_at is not None and record.expires_at <= now:
        return None

    record.last_used_at = now
    db.add(record)
    db.commit()
    return db.get(Customer, record.customer_id)


def current_admin(customer: Customer = Depends(current_customer)) -> Customer:
    if not customer.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
    return customer


def get_owned_server(
    server_id: uuid.UUID,
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> Server:
    """Fetch a server the caller is entitled to act on.

    A server the customer does not hold returns 404 rather than 403: whether a
    given serial exists in the fleet is not their business.
    """
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    if customer.is_admin:
        return server

    owns = db.execute(
        select(Subscription.id).where(
            Subscription.server_id == server.id,
            Subscription.customer_id == customer.id,
            Subscription.ended_at.is_(None),
        )
    ).scalar_one_or_none()
    if owns is None:
        raise HTTPException(status_code=404, detail="server not found")
    return server


def get_owned_job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if customer.is_admin:
        return job
    if job.server_id is None:
        raise HTTPException(status_code=404, detail="job not found")

    owns = db.execute(
        select(Subscription.id).where(
            Subscription.server_id == job.server_id,
            Subscription.customer_id == customer.id,
            Subscription.ended_at.is_(None),
        )
    ).scalar_one_or_none()
    if owns is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def client_ip(request: Request) -> str | None:
    """Caller address for the audit log, or None if it is not an IP.

    `X-Forwarded-For` is honoured only when `trust_proxy_headers` is set, which
    should be true exactly when the API sits behind a reverse proxy we control
    and false when it is reachable directly.

    The result is validated before it is returned: the audit column is `INET`,
    and a peer name that is not an address — a unix socket, a test transport,
    a malformed forwarded header — would otherwise turn an audit write into a
    500 on an endpoint that had already done its work.
    """
    forwarded = request.headers.get("x-forwarded-for") if settings.trust_proxy_headers else None
    candidate = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else None)
    )
    if not candidate:
        return None
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate
