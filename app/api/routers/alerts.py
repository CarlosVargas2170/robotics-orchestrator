import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.backend import CurrentUser
from app.auth.deps import get_current_user
from app.db.models import Alert, Camera
from app.db.session import get_session
from app.schemas.api import AcknowledgeIn, AlertOut

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _out(alert: Alert, robot_code: str | None) -> AlertOut:
    return AlertOut.model_validate(alert).model_copy(update={"robot_code": robot_code})


async def _get_visible(session: AsyncSession, user: CurrentUser, alert_id: uuid.UUID) -> tuple[Alert, str | None]:
    stmt = select(Alert, Camera.robot_code).join(Camera, Camera.id == Alert.camera_id).where(Alert.id == alert_id)
    if not user.is_admin:
        stmt = stmt.where(Camera.owner_user_id == user.id)
    row = (await session.execute(stmt)).first()
    if row is None:
        raise HTTPException(404, "Alerta no encontrada")
    return row[0], row[1]


@router.get("", response_model=list[AlertOut])
async def list_alerts(
    acknowledged: bool | None = None,
    camera_id: uuid.UUID | None = None,
    alert: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Alertas confirmadas de las camaras del usuario (el admin ve todas), de la mas reciente a la mas antigua.

    `acknowledged=false` devuelve las pendientes de atender.
    """
    stmt = (
        select(Alert, Camera.robot_code)
        .join(Camera, Camera.id == Alert.camera_id)
        .order_by(Alert.started_at.desc(), Alert.id)
        .limit(max(1, min(limit, 200)))
        .offset(max(0, offset))
    )
    if not user.is_admin:
        stmt = stmt.where(Camera.owner_user_id == user.id)
    if acknowledged is not None:
        stmt = stmt.where(Alert.acknowledged_at.is_not(None) if acknowledged else Alert.acknowledged_at.is_(None))
    if camera_id is not None:
        stmt = stmt.where(Alert.camera_id == camera_id)
    if alert:
        stmt = stmt.where(Alert.alert == alert)
    if since is not None:
        stmt = stmt.where(Alert.started_at >= since)
    if until is not None:
        stmt = stmt.where(Alert.started_at < until)
    return [_out(a, robot_code) for a, robot_code in (await session.execute(stmt)).all()]


@router.get("/{alert_id}", response_model=AlertOut)
async def get_alert(
    alert_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    return _out(*await _get_visible(session, user, alert_id))


@router.post("/{alert_id}/acknowledge", response_model=AlertOut)
async def acknowledge_alert(
    alert_id: uuid.UUID,
    request: Request,
    body: AcknowledgeIn | None = None,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Marca la alerta como atendida. Si ya lo estaba, no se sobrescribe quien la atendio primero."""
    alert, robot_code = await _get_visible(session, user, alert_id)
    if alert.acknowledged_at is not None:
        return _out(alert, robot_code)

    alert.acknowledged_at = datetime.now(UTC)
    alert.acknowledged_by = user.id
    alert.note = body.note if body else None
    audit.record(session, user, "alert.acknowledge", "alert", alert.id, request, {"note": alert.note})
    await session.commit()

    # Para que las demas pantallas abiertas quiten la alerta de su lista
    publisher = getattr(request.app.state, "publisher", None)
    if publisher is not None:
        await publisher.publish(json.dumps({
            "type": "alert_acknowledged", "alert_id": str(alert.id), "camera_id": str(alert.camera_id),
            "clip_id": str(alert.clip_id), "acknowledged_by": str(user.id),
            "acknowledged_at": alert.acknowledged_at.isoformat(),
        }))
    return _out(alert, robot_code)
