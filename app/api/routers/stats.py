import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import as_utc
from app.auth.backend import CurrentUser
from app.auth.deps import get_current_user
from app.db.expressions import DETECTED_AT
from app.db.models import Alert, Camera, Clip, ClipStatus, Detection, Detector
from app.db.session import get_session
from app.schemas.api import (
    AlertStats,
    ClipStats,
    DayCount,
    DetectionStats,
    RobotCount,
    ServiceCount,
    StatsOut,
    TypeCount,
)

router = APIRouter(prefix="/stats", tags=["stats"])

DEFAULT_WINDOW = timedelta(days=7)
MAX_WINDOW = timedelta(days=366)


@router.get("/summary", response_model=StatsOut)
async def summary(
    since: datetime | None = None,
    until: datetime | None = None,
    tz: str = "UTC",
    camera_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Resumen del historial para pantallas y graficos, solo de las camaras del usuario (el admin ve todas).

    Cuenta `since` (incluido) hasta `until` (excluido); por defecto, los ultimos 7 dias. `tz` es la zona horaria
    (p. ej. America/La_Paz) con la que se agrupa por dia.
    """
    until = as_utc(until) or datetime.now(UTC)
    since = as_utc(since) or until - DEFAULT_WINDOW
    if since >= until:
        raise HTTPException(422, "since debe ser anterior a until")
    if until - since > MAX_WINDOW:
        raise HTTPException(422, "El rango maximo es de 366 dias")
    # Quien agrupa por dia es PostgreSQL, asi que es su base de zonas horarias la que decide si `tz` vale
    query = text("SELECT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = :tz)")
    known = await session.scalar(query, {"tz": tz})
    if not known:
        raise HTTPException(422, f"Zona horaria desconocida: {tz}")

    def scoped(stmt):
        if not user.is_admin:
            stmt = stmt.where(Camera.owner_user_id == user.id)
        return stmt.where(Camera.id == camera_id) if camera_id is not None else stmt

    def local_day(column):
        return func.date(func.timezone(tz, column))

    async def rows(stmt):
        return (await session.execute(stmt)).all()

    # --- Alertas
    in_range = (Alert.started_at >= since, Alert.started_at < until)

    def alert_query(*columns):
        stmt = select(*columns).select_from(Alert).join(Camera, Camera.id == Alert.camera_id)
        return scoped(stmt).where(*in_range)

    alert_total = await session.scalar(alert_query(func.count(Alert.id)))
    alert_pending = await session.scalar(alert_query(func.count(Alert.id)).where(Alert.acknowledged_at.is_(None)))
    alert_day = local_day(Alert.started_at).label("day")
    alert_days = await rows(alert_query(alert_day, func.count()).group_by(alert_day).order_by(alert_day))
    alert_types = await rows(
        alert_query(Alert.alert, func.count().label("n"))
        .group_by(Alert.alert)
        .order_by(func.count().desc(), Alert.alert)
    )
    alert_robots = await rows(
        alert_query(Camera.id, Camera.name, Camera.robot_code, func.count().label("n"))
        .group_by(Camera.id, Camera.name, Camera.robot_code)
        .order_by(func.count().desc(), Camera.name)
    )

    # --- Detecciones (por su instante real)
    def detection_query(*columns):
        stmt = (
            select(*columns)
            .select_from(Detection)
            .join(Clip, Clip.id == Detection.clip_id)
            .join(Camera, Camera.id == Clip.camera_id)
            .join(Detector, Detector.id == Detection.detector_id)
        )
        return scoped(stmt).where(DETECTED_AT >= since, DETECTED_AT < until)

    detection_total = await session.scalar(detection_query(func.count(Detection.id)))
    detection_services = await rows(
        detection_query(Detector.id, Detector.name, Detector.kind, func.count().label("n"))
        .group_by(Detector.id, Detector.name, Detector.kind)
        .order_by(func.count().desc(), Detector.name)
    )
    detection_day = local_day(DETECTED_AT).label("day")
    detection_days = await rows(
        detection_query(detection_day, func.count()).group_by(detection_day).order_by(detection_day)
    )

    # --- Clips
    clip_statuses = await rows(
        scoped(select(Clip.status, func.count()).select_from(Clip).join(Camera, Camera.id == Clip.camera_id))
        .where(Clip.started_at >= since, Clip.started_at < until)
        .group_by(Clip.status)
    )
    by_status = {status.value: 0 for status in ClipStatus} | {status.value: n for status, n in clip_statuses}

    return StatsOut(
        since=since, until=until, tz=tz,
        alerts=AlertStats(
            total=alert_total, pending=alert_pending,
            by_day=[DayCount(day=d, count=n) for d, n in alert_days],
            by_type=[TypeCount(alert=a, count=n) for a, n in alert_types],
            by_robot=[RobotCount(camera_id=i, name=name, robot_code=code, count=n)
                      for i, name, code, n in alert_robots],
        ),
        detections=DetectionStats(
            total=detection_total,
            by_service=[ServiceCount(detector_id=i, name=name, kind=kind, count=n)
                        for i, name, kind, n in detection_services],
            by_day=[DayCount(day=d, count=n) for d, n in detection_days],
        ),
        clips=ClipStats(total=sum(by_status.values()), by_status=by_status),
    )
