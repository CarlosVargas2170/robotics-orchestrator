import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import as_utc, page, set_total
from app.auth.backend import CurrentUser
from app.auth.deps import get_current_user
from app.db.expressions import DETECTED_AT
from app.db.models import Camera, Clip, Detection, Detector
from app.db.session import get_session
from app.schemas.api import DetectionOut

router = APIRouter(prefix="/detections", tags=["detections"])


@router.get("", response_model=list[DetectionOut])
async def list_detections(
    response: Response,
    since: datetime | None = None,
    until: datetime | None = None,
    camera_id: uuid.UUID | None = None,
    clip_id: uuid.UUID | None = None,
    detector_id: uuid.UUID | None = None,
    kind: str | None = None,
    label: str | None = None,
    alert: str | None = None,
    alert_confirmed: bool | None = None,
    min_confidence: float = 0.0,
    limit: int = 100,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Historial de detecciones de las camaras del usuario (el admin ve todas), de la mas reciente a la mas antigua.

    `since`/`until` filtran por el instante real de la deteccion. `kind` es el tipo de servicio (persons, faces...).
    El total de resultados, sin paginar, va en la cabecera `X-Total-Count`.
    """
    since, until = as_utc(since), as_utc(until)
    limit, offset = page(limit, offset, max_limit=500)

    def build(*columns):
        stmt = (
            select(*columns)
            .select_from(Detection)
            .join(Clip, Clip.id == Detection.clip_id)
            .join(Camera, Camera.id == Clip.camera_id)
            .join(Detector, Detector.id == Detection.detector_id)
            .where(Detection.confidence >= min_confidence)
        )
        if not user.is_admin:
            stmt = stmt.where(Camera.owner_user_id == user.id)
        filters = (
            (since, lambda: DETECTED_AT >= since),
            (until, lambda: DETECTED_AT < until),
            (camera_id, lambda: Clip.camera_id == camera_id),
            (clip_id, lambda: Detection.clip_id == clip_id),
            (detector_id, lambda: Detection.detector_id == detector_id),
            (kind, lambda: Detector.kind == kind),
            (label, lambda: Detection.label == label),
            (alert, lambda: Detection.alert == alert),
        )
        for value, condition in filters:
            if value is not None and value != "":
                stmt = stmt.where(condition())
        if alert_confirmed is not None:
            stmt = stmt.where(Detection.alert_confirmed.is_(alert_confirmed))
        return stmt

    set_total(response, await session.scalar(build(func.count(Detection.id))))
    rows = (await session.execute(
        build(Detection, Detector.name, Detector.kind, DETECTED_AT.label("detected_at"),
              Clip.camera_id, Camera.robot_code)
        .order_by(DETECTED_AT.desc(), Detection.id.desc())
        .limit(limit).offset(offset)
    )).all()
    return [
        DetectionOut.model_validate(det).model_copy(update={
            "detector_name": name, "detector_kind": detector_kind, "detected_at": detected_at,
            "camera_id": cam_id, "robot_code": robot_code,
        })
        for det, name, detector_kind, detected_at, cam_id, robot_code in rows
    ]
