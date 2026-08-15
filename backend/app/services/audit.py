"""Audit trail helper.

Deliberately dumb: one function, called from anywhere something happens that
a human might later have to explain to a customer or an upstream provider.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.enums import ActorType
from app.models import AuditLog


def record_audit(
    db: Session,
    *,
    action: str,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: str | uuid.UUID | None = None,
    actor_label: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    source_ip: str | None = None,
    user_agent: str | None = None,
    detail: dict | None = None,
) -> AuditLog:
    entry = AuditLog(
        action=action,
        actor_type=actor_type,
        actor_id=uuid.UUID(str(actor_id)) if actor_id else None,
        actor_label=actor_label,
        target_type=target_type,
        target_id=target_id,
        source_ip=source_ip,
        user_agent=(user_agent or "")[:512] or None,
        detail=detail or {},
    )
    db.add(entry)
    return entry
