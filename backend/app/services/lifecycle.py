"""Server lifecycle state machine."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.enums import SERVER_TRANSITIONS, ActorType, ServerState
from app.models import Server
from app.services.audit import record_audit


class IllegalTransition(ValueError):
    def __init__(self, current: ServerState, target: ServerState) -> None:
        super().__init__(f"cannot move server from {current.value} to {target.value}")
        self.current = current
        self.target = target


def can_transition(current: ServerState, target: ServerState) -> bool:
    return target in SERVER_TRANSITIONS.get(current, set())


def transition_server(
    db: Session,
    server: Server,
    target: ServerState,
    *,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: str | None = None,
    actor_label: str | None = None,
    reason: str | None = None,
) -> Server:
    """Move a server to `target`, or raise.

    Every transition is audited. A server that ends up somewhere unexpected
    should always be explainable from this table.
    """
    current = ServerState(server.state)
    if current == target:
        return server
    if not can_transition(current, target):
        raise IllegalTransition(current, target)

    server.state = target
    server.state_changed_at = datetime.now(UTC)

    if target is ServerState.IN_STOCK:
        # A server only re-enters the sellable pool through a wipe, so drop
        # any stale customer association at the same moment.
        server.hostname = None

    record_audit(
        db,
        action="server.state_change",
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        target_type="server",
        target_id=str(server.id),
        detail={"from": current.value, "to": target.value, "reason": reason},
    )
    db.add(server)
    return server
