from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import CurrentUser
from app.db.models import AuditLog


def record(
    session: AsyncSession,
    user: CurrentUser,
    action: str,
    entity_type: str,
    entity_id: object,
    request: Request | None = None,
    details: dict | None = None,
) -> None:
    """Anota una accion de un usuario; se confirma junto con la transaccion de quien llama."""
    session.add(AuditLog(
        actor_type="user", actor_id=str(user.id), action=action, entity_type=entity_type,
        entity_id=str(entity_id), ip=request.client.host if request and request.client else None,
        details=details,
    ))
