from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Camera
from app.db.session import get_session
from app.schemas.api import CameraIn, CameraOut

router = APIRouter(prefix="/cameras", tags=["cameras"])


@router.get("", response_model=list[CameraOut])
async def list_cameras(session: AsyncSession = Depends(get_session)):
    return (await session.execute(select(Camera).order_by(Camera.id))).scalars().all()


@router.post("", response_model=CameraOut, status_code=201)
async def create_camera(body: CameraIn, session: AsyncSession = Depends(get_session)):
    camera = Camera(**body.model_dump())
    session.add(camera)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Ya existe una camara con ese stream_name") from None
    return camera
