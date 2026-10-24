import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Detection
from app.db.session import get_session
from app.schemas.api import DetectionOut

router = APIRouter(prefix="/detections", tags=["detections"])


@router.get("", response_model=list[DetectionOut])
async def list_detections(
    clip_id: uuid.UUID | None = None,
    detector_id: uuid.UUID | None = None,
    label: str | None = None,
    alert: str | None = None,
    min_confidence: float = 0.0,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Detection)
        .where(Detection.confidence >= min_confidence)
        .order_by(Detection.id.desc())
        .limit(min(limit, 500))
    )
    if clip_id is not None:
        stmt = stmt.where(Detection.clip_id == clip_id)
    if detector_id is not None:
        stmt = stmt.where(Detection.detector_id == detector_id)
    if label:
        stmt = stmt.where(Detection.label == label)
    if alert:
        stmt = stmt.where(Detection.alert == alert)
    return (await session.execute(stmt)).scalars().all()
