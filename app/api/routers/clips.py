import uuid

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.api.deps import get_queue
from app.auth.access import get_visible_camera
from app.auth.backend import CurrentUser
from app.auth.deps import get_current_user
from app.db.models import Camera, Clip
from app.db.session import get_session
from app.schemas.api import ClipIn, ClipOut

router = APIRouter(prefix="/clips", tags=["clips"])


@router.post("", response_model=ClipOut, status_code=202)
async def request_clip(
    body: ClipIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    queue: ArqRedis = Depends(get_queue),
    user: CurrentUser = Depends(get_current_user),
):
    """Registra un clip de Flussonic de una camara del usuario y lo encola para su procesamiento."""
    if await get_visible_camera(session, user, body.camera_id) is None:
        raise HTTPException(404, "Camara no encontrada")
    clip = Clip(**body.model_dump(), requested_by=user.id)
    session.add(clip)
    await session.flush()
    audit.record(session, user, "clip.request", "clip", clip.id, request, {"camera_id": str(body.camera_id)})
    await session.commit()
    await queue.enqueue_job("process_clip_job", str(clip.id))
    return clip


@router.get("", response_model=list[ClipOut])
async def list_clips(
    camera_id: uuid.UUID | None = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    stmt = (
        select(Clip)
        .join(Camera, Camera.id == Clip.camera_id)
        .order_by(Clip.created_at.desc())
        .limit(min(limit, 200))
    )
    if not user.is_admin:
        stmt = stmt.where(Camera.owner_user_id == user.id)
    if camera_id is not None:
        stmt = stmt.where(Clip.camera_id == camera_id)
    return (await session.execute(stmt)).scalars().all()


@router.get("/{clip_id}", response_model=ClipOut)
async def get_clip(
    clip_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    clip = await session.get(Clip, clip_id)
    if clip is None or await get_visible_camera(session, user, clip.camera_id) is None:
        raise HTTPException(404, "Clip no encontrado")
    return clip
