from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Camera, CameraDetector, Detector, DetectorAccess
from app.detectors.http import HttpDetector


async def detectors_for_camera(session: AsyncSession, camera: Camera) -> list[Detector]:
    """Servicios a los que se envian los frames de una camara.

    = activados en la camara  ∩  habilitados globalmente  ∩  permitidos al dueño de la camara
    (si la camara no tiene dueño, no se filtra por permisos de usuario).
    """
    stmt = (
        select(Detector)
        .join(CameraDetector, CameraDetector.detector_id == Detector.id)
        .where(
            CameraDetector.camera_id == camera.id,
            CameraDetector.enabled.is_(True),
            Detector.enabled.is_(True),
        )
        .order_by(Detector.name)
    )
    if camera.owner_user_id is not None:
        stmt = stmt.join(
            DetectorAccess,
            (DetectorAccess.detector_id == Detector.id) & (DetectorAccess.user_id == camera.owner_user_id),
        ).where(DetectorAccess.enabled.is_(True))
    return list((await session.execute(stmt)).scalars().all())


class DetectorPool:
    """Reutiliza un cliente HTTP por servicio y lo recrea si cambia su configuracion en la BD."""

    def __init__(self) -> None:
        self._cache: dict = {}
        self._stale: list[HttpDetector] = []

    def get(self, row: Detector) -> HttpDetector:
        signature = (row.base_url, row.timeout_s, row.kind, row.min_confidence, row.name)
        cached = self._cache.get(row.id)
        if cached is None or cached[0] != signature:
            if cached is not None:
                self._stale.append(cached[1])
            cached = (
                signature,
                HttpDetector(
                    row.name, row.base_url, row.timeout_s,
                    id=row.id, kind=row.kind, min_confidence=row.min_confidence,
                ),
            )
            self._cache[row.id] = cached
        return cached[1]

    async def aclose(self) -> None:
        for _, detector in self._cache.values():
            await detector.aclose()
        for detector in self._stale:
            await detector.aclose()
        self._cache.clear()
        self._stale.clear()
