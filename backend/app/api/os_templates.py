"""The OS list customers may install from."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_customer
from app.models import Customer, OSTemplate
from app.schemas import OSTemplateOut

router = APIRouter(prefix="/api/v1/os-templates", tags=["os-templates"])


@router.get("", response_model=list[OSTemplateOut])
def list_templates(
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> list[OSTemplate]:
    """Enabled templates.

    The rescue and wipe images are `is_public=False`: they back internal rails
    and must not appear as things a customer can choose to install.
    """
    query = select(OSTemplate).where(OSTemplate.is_enabled.is_(True))
    if not customer.is_admin:
        query = query.where(OSTemplate.is_public.is_(True))
    return list(
        db.execute(query.order_by(OSTemplate.sort_order, OSTemplate.name)).scalars().all()
    )
