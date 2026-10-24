from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_queue
from app.db.models import Camera, Clip
from app.db.session import get_session
from app.schemas.api import ClipIn, ClipOut

router = APIRouter(prefix="/clips", tags=["clips"])


@router.post("", response_model=ClipOut, status_code=202)
async def request_clip(
    body: ClipIn,
    session: AsyncSession = Depends(get_session),
    queue: ArqRedis = Depends(get_queue),
):
    """Registra un clip de Flussonic y lo encola para su procesamiento."""
    if await session.get(Camera, body.camera_id) is None:
        raise HTTPException(404, "Camara no encontrada")
    clip = Clip(**body.model_dump())
    session.add(clip)
    await session.commit()
    await queue.enqueue_job("process_clip_job", clip.id)
    return clip


@router.get("", response_model=list[ClipOut])
async def list_clips(
    camera_id: int | None = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Clip).order_by(Clip.id.desc()).limit(min(limit, 200))
    if camera_id is not None:
        stmt = stmt.where(Clip.camera_id == camera_id)
    return (await session.execute(stmt)).scalars().all()


@router.get("/{clip_id}", response_model=ClipOut)
async def get_clip(clip_id: int, session: AsyncSession = Depends(get_session)):
    clip = await session.get(Clip, clip_id)
    if clip is None:
        raise HTTPException(404, "Clip no encontrado")
    return clip
