import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import CurrentUser
from app.auth.deps import require_admin
from app.db.models import Camera, CameraDetector, Detector, DetectorAccess
from app.db.session import get_session
from app.schemas.api import ActiveOn, UserDetectorOut

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/{user_id}/detectors", response_model=list[UserDetectorOut])
async def user_detectors(
    user_id: uuid.UUID, session: AsyncSession = Depends(get_session), _: CurrentUser = Depends(require_admin)
):
    """Todos los servicios, indicando cuales puede usar este usuario y en que robots suyos estan encendidos.

    `user_id` es el id en robotics-backend. No se valida que exista, para poder revisar a un usuario ya borrado.
    """
    detectors = (await session.execute(select(Detector).order_by(Detector.name))).scalars().all()
    access = {
        a.detector_id: a.enabled
        for a in (await session.execute(select(DetectorAccess).where(DetectorAccess.user_id == user_id))).scalars()
    }
    active: dict[uuid.UUID, list[ActiveOn]] = {}
    links = await session.execute(
        select(CameraDetector.detector_id, Camera.id, Camera.name, Camera.robot_code)
        .join(Camera, Camera.id == CameraDetector.camera_id)
        .where(Camera.owner_user_id == user_id, CameraDetector.enabled.is_(True))
        .order_by(Camera.name)
    )
    for detector_id, camera_id, name, robot_code in links.all():
        active.setdefault(detector_id, []).append(ActiveOn(camera_id=camera_id, name=name, robot_code=robot_code))

    return [
        UserDetectorOut(
            detector_id=d.id, name=d.name, kind=d.kind, enabled=d.enabled,
            allowed=access.get(d.id) is True, suspended=access.get(d.id) is False,
            active_on=active.get(d.id, []),
        )
        for d in detectors
    ]
