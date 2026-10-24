import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.api.pagination import as_utc, page, set_total
from app.auth.backend import CurrentUser
from app.auth.deps import get_current_user
from app.db.models import Alert, Camera, Detector
from app.db.session import get_session
from app.schemas.api import AcknowledgeIn, AlertOut

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _out(alert: Alert, robot_code: str | None, detector_name: str | None, detector_kind: str | None) -> AlertOut:
    return AlertOut.model_validate(alert).model_copy(update={
        "robot_code": robot_code, "detector_name": detector_name, "detector_kind": detector_kind,
    })


async def _get_visible(session: AsyncSession, user: CurrentUser, alert_id: uuid.UUID) -> tuple:
    """(alerta, robot_code, nombre del servicio, tipo del servicio); 404 si no es de las camaras del usuario."""
    stmt = (
        select(Alert, Camera.robot_code, Detector.name, Detector.kind)
        .join(Camera, Camera.id == Alert.camera_id)
        .join(Detector, Detector.id == Alert.detector_id)
        .where(Alert.id == alert_id)
    )
    if not user.is_admin:
        stmt = stmt.where(Camera.owner_user_id == user.id)
    row = (await session.execute(stmt)).first()
    if row is None:
        raise HTTPException(404, "Alerta no encontrada")
    return tuple(row)


@router.get("", response_model=list[AlertOut])
async def list_alerts(
    response: Response,
    acknowledged: bool | None = None,
    camera_id: uuid.UUID | None = None,
    alert: str | None = None,
    kind: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Alertas confirmadas de las camaras del usuario (el admin ve todas), de la mas reciente a la mas antigua.

    `acknowledged=false` devuelve las pendientes de atender; `kind` es el tipo de servicio que la origino.
    El total, sin paginar, va en la cabecera `X-Total-Count`.
    """
    since, until = as_utc(since), as_utc(until)
    limit, offset = page(limit, offset, max_limit=200)

    def build(*columns):
        stmt = (
            select(*columns)
            .select_from(Alert)
            .join(Camera, Camera.id == Alert.camera_id)
            .join(Detector, Detector.id == Alert.detector_id)
        )
        if not user.is_admin:
            stmt = stmt.where(Camera.owner_user_id == user.id)
        if acknowledged is not None:
            stmt = stmt.where(Alert.acknowledged_at.is_not(None) if acknowledged else Alert.acknowledged_at.is_(None))
        filters = (
            (camera_id, lambda: Alert.camera_id == camera_id),
            (alert, lambda: Alert.alert == alert),
            (kind, lambda: Detector.kind == kind),
            (since, lambda: Alert.started_at >= since),
            (until, lambda: Alert.started_at < until),
        )
        for value, condition in filters:
            if value is not None and value != "":
                stmt = stmt.where(condition())
        return stmt

    set_total(response, await session.scalar(build(func.count(Alert.id))))
    rows = (await session.execute(
        build(Alert, Camera.robot_code, Detector.name, Detector.kind)
        .order_by(Alert.started_at.desc(), Alert.id)
        .limit(limit)
        .offset(offset)
    )).all()
    return [_out(*row) for row in rows]


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
    alert, *extra = await _get_visible(session, user, alert_id)
    if alert.acknowledged_at is not None:
        return _out(alert, *extra)

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
    return _out(alert, *extra)
