import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import CurrentUser
from app.auth.deps import get_current_user
from app.db.models import Camera, Clip, Detection
from app.db.session import get_session
from app.schemas.api import DetectionOut

router = APIRouter(prefix="/detections", tags=["detections"])


@router.get("", response_model=list[DetectionOut])
async def list_detections(
    clip_id: uuid.UUID | None = None,
    detector_id: uuid.UUID | None = None,
    label: str | None = None,
    alert: str | None = None,
    alert_confirmed: bool | None = None,
    min_confidence: float = 0.0,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Detecciones de las camaras del usuario; el admin ve todas."""
    stmt = (
        select(Detection)
        .join(Clip, Clip.id == Detection.clip_id)
        .join(Camera, Camera.id == Clip.camera_id)
        .where(Detection.confidence >= min_confidence)
        .order_by(Detection.id.desc())
        .limit(min(limit, 500))
    )
    if not user.is_admin:
        stmt = stmt.where(Camera.owner_user_id == user.id)
    if clip_id is not None:
        stmt = stmt.where(Detection.clip_id == clip_id)
    if detector_id is not None:
        stmt = stmt.where(Detection.detector_id == detector_id)
    if label:
        stmt = stmt.where(Detection.label == label)
    if alert:
        stmt = stmt.where(Detection.alert == alert)
    if alert_confirmed is not None:
        stmt = stmt.where(Detection.alert_confirmed.is_(alert_confirmed))
    return (await session.execute(stmt)).scalars().all()
