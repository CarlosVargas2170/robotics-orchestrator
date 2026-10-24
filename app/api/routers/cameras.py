from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.access import visible_cameras
from app.auth.backend import CurrentUser
from app.auth.deps import get_current_user, require_admin
from app.db.models import Camera
from app.db.session import get_session
from app.schemas.api import CameraIn, CameraOut

router = APIRouter(prefix="/cameras", tags=["cameras"])


@router.get("", response_model=list[CameraOut])
async def list_cameras(
    session: AsyncSession = Depends(get_session), user: CurrentUser = Depends(get_current_user)
):
    """Las camaras del usuario; el admin ve todas."""
    stmt = visible_cameras(select(Camera).order_by(Camera.name), user)
    return (await session.execute(stmt)).scalars().all()


@router.post("", response_model=CameraOut, status_code=201)
async def create_camera(
    body: CameraIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: CurrentUser = Depends(require_admin),
):
    """Solo el admin da de alta camaras y asigna su dueño (`owner_user_id`)."""
    camera = Camera(**body.model_dump())
    session.add(camera)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Ya existe una camara con ese stream_name") from None
    audit.record(session, admin, "camera.create", "camera", camera.id, request,
                 {"stream_name": camera.stream_name, "owner_user_id": str(camera.owner_user_id)})
    await session.commit()
    return camera
