import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.access import get_visible_camera, visible_cameras
from app.auth.backend import CurrentUser
from app.auth.deps import get_current_user, require_admin
from app.db.models import Camera, CameraDetector, Detector, DetectorAccess
from app.db.session import get_session
from app.schemas.api import CameraDetectorIn, CameraDetectorOut, CameraIn, CameraOut, CameraPatch

router = APIRouter(prefix="/cameras", tags=["cameras"])


async def _camera_or_404(session: AsyncSession, user: CurrentUser, camera_id: uuid.UUID) -> Camera:
    camera = await get_visible_camera(session, user, camera_id)
    if camera is None:
        raise HTTPException(404, "Camara no encontrada")
    return camera


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


@router.patch("/{camera_id}", response_model=CameraOut)
async def update_camera(
    camera_id: uuid.UUID,
    body: CameraPatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: CurrentUser = Depends(require_admin),
):
    """Edita una camara o la reasigna a otro usuario (solo admin)."""
    camera = await _camera_or_404(session, admin, camera_id)
    changes = body.model_dump(exclude_unset=True)
    not_nullable = ("name", "enabled", "auto_ingest", "ingest_interval_s", "ingest_duration_s")
    if any(f in changes and changes[f] is None for f in not_nullable):
        raise HTTPException(422, f"Estos campos no admiten null: {', '.join(not_nullable)}")
    interval = changes.get("ingest_interval_s", camera.ingest_interval_s)
    duration = changes.get("ingest_duration_s", camera.ingest_duration_s)
    if interval < duration:
        raise HTTPException(422, "ingest_interval_s no puede ser menor que ingest_duration_s")
    for field, value in changes.items():
        setattr(camera, field, value)
    audit.record(session, admin, "camera.update", "camera", camera.id, request,
                 {k: str(v) for k, v in changes.items()})
    await session.commit()
    return camera


@router.get("/{camera_id}/detectors", response_model=list[CameraDetectorOut])
async def list_camera_detectors(
    camera_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Servicios configurados en la camara y si estan activos."""
    await _camera_or_404(session, user, camera_id)
    stmt = (
        select(Detector.id, Detector.name, Detector.kind, CameraDetector.enabled)
        .join(CameraDetector, CameraDetector.detector_id == Detector.id)
        .where(CameraDetector.camera_id == camera_id)
        .order_by(Detector.name)
    )
    return [
        CameraDetectorOut(detector_id=i, name=n, kind=k, enabled=e)
        for i, n, k, e in (await session.execute(stmt)).all()
    ]


@router.put("/{camera_id}/detectors/{detector_id}", response_model=CameraDetectorOut)
async def set_camera_detector(
    camera_id: uuid.UUID,
    detector_id: uuid.UUID,
    request: Request,
    body: CameraDetectorIn | None = None,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Activa (o apaga, con enabled=false) un servicio en la camara.

    El dueño de la camara debe tener permiso sobre el servicio; si la camara no tiene dueño no se exige.
    """
    camera = await _camera_or_404(session, user, camera_id)
    detector = await session.get(Detector, detector_id)
    if detector is None:
        raise HTTPException(404, "Servicio no encontrado")
    enabled = body.enabled if body else True

    if enabled:
        if not detector.enabled:
            raise HTTPException(409, "El servicio esta deshabilitado")
        if camera.owner_user_id is not None:
            access = await session.get(DetectorAccess, (detector_id, camera.owner_user_id))
            if access is None or not access.enabled:
                if user.is_admin:
                    raise HTTPException(409, "El dueño de la camara no tiene permiso sobre este servicio")
                raise HTTPException(403, "No tienes permiso sobre este servicio")

    link = await session.get(CameraDetector, (camera_id, detector_id))
    if link is None:
        link = CameraDetector(camera_id=camera_id, detector_id=detector_id, enabled=enabled)
        session.add(link)
    else:
        link.enabled = enabled
    audit.record(session, user, "camera.detector.set", "camera", camera_id, request,
                 {"detector_id": str(detector_id), "enabled": enabled})
    await session.commit()
    return CameraDetectorOut(detector_id=detector.id, name=detector.name, kind=detector.kind, enabled=enabled)


@router.delete("/{camera_id}/detectors/{detector_id}", status_code=204)
async def remove_camera_detector(
    camera_id: uuid.UUID,
    detector_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    await _camera_or_404(session, user, camera_id)
    link = await session.get(CameraDetector, (camera_id, detector_id))
    if link is None:
        raise HTTPException(404, "La camara no tiene ese servicio")
    audit.record(session, user, "camera.detector.remove", "camera", camera_id, request,
                 {"detector_id": str(detector_id)})
    await session.delete(link)
    await session.commit()
