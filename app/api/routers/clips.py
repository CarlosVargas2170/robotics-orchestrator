import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.api.pagination import as_utc, page, set_total
from app.auth.access import get_visible_camera
from app.auth.backend import CurrentUser
from app.auth.deps import get_current_user
from app.db.models import Camera, Clip, ClipSource, ClipStatus
from app.db.session import get_session
from app.queue import notify_new_work
from app.schemas.api import ClipIn, ClipOut

router = APIRouter(prefix="/clips", tags=["clips"])


def _out(clip: Clip, robot_code: str | None) -> ClipOut:
    return ClipOut.model_validate(clip).model_copy(update={"robot_code": robot_code})


@router.post("", response_model=ClipOut, status_code=202)
async def request_clip(
    body: ClipIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Registra un clip de Flussonic de una camara del usuario; queda en cola (pending) hasta que un worker lo tome."""
    camera = await get_visible_camera(session, user, body.camera_id)
    if camera is None:
        raise HTTPException(404, "Camara no encontrada")
    clip = Clip(**body.model_dump(), requested_by=user.id)
    session.add(clip)
    await session.flush()
    audit.record(session, user, "clip.request", "clip", clip.id, request, {"camera_id": str(body.camera_id)})
    await session.commit()
    await notify_new_work(session)
    return _out(clip, camera.robot_code)


@router.get("", response_model=list[ClipOut])
async def list_clips(
    response: Response,
    since: datetime | None = None,
    until: datetime | None = None,
    camera_id: uuid.UUID | None = None,
    status: ClipStatus | None = None,
    source: ClipSource | None = None,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Clips analizados de las camaras del usuario (el admin ve todos), del mas reciente al mas antiguo.

    `since`/`until` filtran por el inicio del clip. El total, sin paginar, va en la cabecera `X-Total-Count`.
    """
    since, until = as_utc(since), as_utc(until)
    limit, offset = page(limit, offset, max_limit=200)

    def build(*columns):
        stmt = select(*columns).select_from(Clip).join(Camera, Camera.id == Clip.camera_id)
        if not user.is_admin:
            stmt = stmt.where(Camera.owner_user_id == user.id)
        filters = (
            (since, lambda: Clip.started_at >= since),
            (until, lambda: Clip.started_at < until),
            (camera_id, lambda: Clip.camera_id == camera_id),
            (status, lambda: Clip.status == status),
            (source, lambda: Clip.source == source),
        )
        for value, condition in filters:
            if value is not None:
                stmt = stmt.where(condition())
        return stmt

    set_total(response, await session.scalar(build(func.count(Clip.id))))
    rows = (await session.execute(
        build(Clip, Camera.robot_code).order_by(Clip.started_at.desc(), Clip.id).limit(limit).offset(offset)
    )).all()
    return [_out(clip, robot_code) for clip, robot_code in rows]


@router.get("/{clip_id}", response_model=ClipOut)
async def get_clip(
    clip_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    clip = await session.get(Clip, clip_id)
    camera = await get_visible_camera(session, user, clip.camera_id) if clip else None
    if camera is None:
        raise HTTPException(404, "Clip no encontrado")
    return _out(clip, camera.robot_code)
