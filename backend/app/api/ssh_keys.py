"""Customer SSH key management."""

from __future__ import annotations

import base64
import binascii
import hashlib
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_customer
from app.models import Customer, SSHKey
from app.schemas import SSHKeyCreate, SSHKeyOut

router = APIRouter(prefix="/api/v1/ssh-keys", tags=["ssh-keys"])


def fingerprint(public_key: str) -> str:
    """OpenSSH SHA256 fingerprint, matching `ssh-keygen -lf`."""
    parts = public_key.split()
    if len(parts) < 2:
        raise ValueError("malformed public key")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("public key body is not valid base64") from exc
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


@router.get("", response_model=list[SSHKeyOut])
def list_keys(
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> list[SSHKey]:
    return list(
        db.execute(
            select(SSHKey).where(SSHKey.customer_id == customer.id).order_by(SSHKey.created_at)
        )
        .scalars()
        .all()
    )


@router.post("", response_model=SSHKeyOut, status_code=status.HTTP_201_CREATED)
def add_key(
    payload: SSHKeyCreate,
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> SSHKey:
    try:
        fp = fingerprint(payload.public_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing = db.execute(
        select(SSHKey).where(SSHKey.customer_id == customer.id, SSHKey.fingerprint == fp)
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="key already added")

    key = SSHKey(
        customer_id=customer.id,
        name=payload.name,
        public_key=payload.public_key.strip(),
        fingerprint=fp,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_key(
    key_id: uuid.UUID,
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> None:
    """Remove a key.

    Only affects future installs — a key already written to a running server's
    authorized_keys stays there until that server is reinstalled.
    """
    key = db.get(SSHKey, key_id)
    if key is None or key.customer_id != customer.id:
        raise HTTPException(status_code=404, detail="key not found")
    db.delete(key)
    db.commit()
