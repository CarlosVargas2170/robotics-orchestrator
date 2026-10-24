import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.backend import CurrentUser
from app.auth.deps import get_current_user, require_admin
from app.auth.directory import ensure_product_user
from app.db.models import Detection, Detector, DetectorAccess
from app.db.session import get_session
from app.detectors import health
from app.detectors.adapters import ADAPTERS
from app.schemas.api import (
    AccessIn,
    AccessOut,
    AdapterOut,
    DetectorIn,
    DetectorOut,
    DetectorPatch,
    DetectorSummary,
)

router = APIRouter(prefix="/detectors", tags=["detectors"])


async def _get_or_404(session: AsyncSession, detector_id: uuid.UUID) -> Detector:
    detector = await session.get(Detector, detector_id)
    if detector is None:
        raise HTTPException(404, "Servicio no encontrado")
    return detector


async def _ensure_name_free(session: AsyncSession, name: str, exclude: uuid.UUID | None = None) -> None:
    taken = await session.scalar(select(Detector.id).where(Detector.name == name))
    if taken is not None and taken != exclude:
        raise HTTPException(409, "Ya existe un servicio con ese nombre")


@router.post("", response_model=DetectorOut, status_code=201)
async def create_detector(
    body: DetectorIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: CurrentUser = Depends(require_admin),
):
    """Registra un servicio de IA (solo admin)."""
    await _ensure_name_free(session, body.name)
    detector = Detector(**body.model_dump())
    session.add(detector)
    await session.flush()
    audit.record(session, admin, "detector.create", "detector", detector.id, request,
                 {"name": detector.name, "base_url": detector.base_url, "adapter": detector.adapter})
    await session.commit()
    return detector


@router.get("", response_model=list[DetectorOut])
async def list_detectors(session: AsyncSession = Depends(get_session), _: CurrentUser = Depends(require_admin)):
    return (await session.execute(select(Detector).order_by(Detector.name))).scalars().all()


# Estas rutas fijas deben ir antes de /{detector_id}
@router.get("/adapters", response_model=list[AdapterOut])
async def list_adapters(_: CurrentUser = Depends(require_admin)):
    """Tipos de servicio que el orquestador sabe leer: el valor de `adapter` al registrar un servicio."""
    return [
        AdapterOut(name=name, description=a.description, analyze_path=a.path, health_path=a.health_path,
                   has_health=a.health_path is not None)
        for name, a in sorted(ADAPTERS.items())
    ]


@router.get("/available", response_model=list[DetectorSummary])
async def available_detectors(
    session: AsyncSession = Depends(get_session), user: CurrentUser = Depends(get_current_user)
):
    """Servicios que el usuario puede activar en sus camaras (el admin ve todos los habilitados)."""
    stmt = select(Detector).where(Detector.enabled.is_(True)).order_by(Detector.name)
    if not user.is_admin:
        stmt = stmt.join(DetectorAccess, DetectorAccess.detector_id == Detector.id).where(
            DetectorAccess.user_id == user.id, DetectorAccess.enabled.is_(True)
        )
    return (await session.execute(stmt)).scalars().all()


@router.get("/{detector_id}", response_model=DetectorOut)
async def get_detector(
    detector_id: uuid.UUID, session: AsyncSession = Depends(get_session), _: CurrentUser = Depends(require_admin)
):
    return await _get_or_404(session, detector_id)


@router.patch("/{detector_id}", response_model=DetectorOut)
async def update_detector(
    detector_id: uuid.UUID,
    body: DetectorPatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: CurrentUser = Depends(require_admin),
):
    detector = await _get_or_404(session, detector_id)
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes:
        await _ensure_name_free(session, changes["name"], exclude=detector.id)
    for field, value in changes.items():
        setattr(detector, field, value)
    audit.record(session, admin, "detector.update", "detector", detector.id, request, changes)
    await session.commit()
    return detector


@router.delete("/{detector_id}", status_code=204)
async def delete_detector(
    detector_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: CurrentUser = Depends(require_admin),
):
    """Elimina un servicio sin historial; si ya tiene detecciones hay que deshabilitarlo (PATCH enabled=false)."""
    detector = await _get_or_404(session, detector_id)
    if await session.scalar(select(Detection.id).where(Detection.detector_id == detector.id).limit(1)) is not None:
        raise HTTPException(409, "El servicio tiene detecciones; deshabilitalo en lugar de eliminarlo")
    audit.record(session, admin, "detector.delete", "detector", detector.id, request, {"name": detector.name})
    await session.delete(detector)
    await session.commit()


@router.get("/{detector_id}/health")
async def detector_health(
    detector_id: uuid.UUID, session: AsyncSession = Depends(get_session), _: CurrentUser = Depends(require_admin)
):
    """Consulta el health check del servicio para comprobar que esta en linea y con su modelo cargado."""
    detector = await _get_or_404(session, detector_id)
    try:
        return await health.probe(detector.base_url, detector.adapter)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/{detector_id}/access", response_model=list[AccessOut])
async def list_access(
    detector_id: uuid.UUID, session: AsyncSession = Depends(get_session), _: CurrentUser = Depends(require_admin)
):
    """Usuarios con permiso para usar el servicio."""
    await _get_or_404(session, detector_id)
    stmt = select(DetectorAccess).where(DetectorAccess.detector_id == detector_id).order_by(DetectorAccess.user_id)
    return (await session.execute(stmt)).scalars().all()


@router.put("/{detector_id}/access/{user_id}", response_model=AccessOut)
async def grant_access(
    detector_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    body: AccessIn | None = None,
    session: AsyncSession = Depends(get_session),
    admin: CurrentUser = Depends(require_admin),
):
    """Da (o suspende, con enabled=false) el permiso de un usuario sobre un servicio.

    `user_id` es el id del usuario en robotics-backend. Al dar o reactivar el permiso se comprueba que exista, este
    activo y use este producto (admin o cliente); suspenderlo no lo exige, para poder limpiar a un usuario ya borrado.
    """
    await _get_or_404(session, detector_id)
    enabled = body.enabled if body else True
    if enabled:
        await ensure_product_user(admin, user_id)
    access = await session.get(DetectorAccess, (detector_id, user_id))
    if access is None:
        access = DetectorAccess(detector_id=detector_id, user_id=user_id, enabled=enabled)
        session.add(access)
    else:
        access.enabled = enabled
    audit.record(session, admin, "detector.access.set", "detector", detector_id, request,
                 {"user_id": str(user_id), "enabled": enabled})
    await session.commit()
    return access


@router.delete("/{detector_id}/access/{user_id}", status_code=204)
async def revoke_access(
    detector_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: CurrentUser = Depends(require_admin),
):
    access = await session.get(DetectorAccess, (detector_id, user_id))
    if access is None:
        raise HTTPException(404, "Ese usuario no tiene permiso sobre el servicio")
    audit.record(session, admin, "detector.access.revoke", "detector", detector_id, request,
                 {"user_id": str(user_id)})
    await session.delete(access)
    await session.commit()
