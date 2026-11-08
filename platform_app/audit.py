"""Append-only application audit, using reviewed metadata rather than bodies."""

from uuid import uuid4
from .models import AuditLog


def append_audit(
    db, actor_id, action, target_type, target_id, *, result="success", request_id=None, details=None
):
    row = AuditLog(
        id=str(uuid4()),
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        result=result,
        request_id=request_id,
        details=details or {},
    )
    db.add(row)
    return row
