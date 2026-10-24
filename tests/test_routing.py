"""Regla de enrutamiento: camara activa ∩ detector habilitado ∩ permiso del dueño.

Usa el Postgres de DATABASE_URL (con las migraciones aplicadas) dentro de una transaccion
que se revierte; se omite si la BD no esta disponible.
"""
import uuid

from app.db.models import Camera, CameraDetector, Detector, DetectorAccess
from app.detectors.registry import detectors_for_camera


def _detector(kind: str, enabled: bool = True) -> Detector:
    return Detector(name=f"{kind}-{uuid.uuid4().hex[:6]}", kind=kind, base_url=f"http://{kind}:9000", enabled=enabled)


async def _names(session, camera) -> set[str]:
    return {d.kind for d in await detectors_for_camera(session, camera)}


async def test_each_user_only_reaches_their_services(session):
    faces, animals, objects = _detector("faces"), _detector("animals"), _detector("objects")
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    cam_a = Camera(name="A", stream_name=f"a-{uuid.uuid4().hex[:6]}", owner_user_id=user_a)
    cam_b = Camera(name="B", stream_name=f"b-{uuid.uuid4().hex[:6]}", owner_user_id=user_b)
    session.add_all([faces, animals, objects, cam_a, cam_b])
    await session.flush()

    # Permisos: A -> rostros + animales; B -> animales + objetos
    session.add_all([
        DetectorAccess(detector_id=faces.id, user_id=user_a),
        DetectorAccess(detector_id=animals.id, user_id=user_a),
        DetectorAccess(detector_id=animals.id, user_id=user_b),
        DetectorAccess(detector_id=objects.id, user_id=user_b),
    ])
    # Ambas camaras activan los tres servicios; el permiso del dueño es el que filtra
    for cam in (cam_a, cam_b):
        session.add_all([CameraDetector(camera_id=cam.id, detector_id=d.id) for d in (faces, animals, objects)])
    await session.flush()

    assert await _names(session, cam_a) == {"faces", "animals"}
    assert await _names(session, cam_b) == {"animals", "objects"}


async def test_camera_activation_and_global_switch_also_filter(session):
    faces, animals = _detector("faces"), _detector("animals", enabled=False)
    user = uuid.uuid4()
    cam = Camera(name="C", stream_name=f"c-{uuid.uuid4().hex[:6]}", owner_user_id=user)
    session.add_all([faces, animals, cam])
    await session.flush()
    session.add_all([
        DetectorAccess(detector_id=faces.id, user_id=user),
        DetectorAccess(detector_id=animals.id, user_id=user),
        CameraDetector(camera_id=cam.id, detector_id=faces.id, enabled=False),  # apagado en la camara
        CameraDetector(camera_id=cam.id, detector_id=animals.id),  # apagado globalmente
    ])
    await session.flush()

    assert await _names(session, cam) == set()


async def test_camera_without_owner_skips_user_permissions(session):
    faces = _detector("faces")
    cam = Camera(name="D", stream_name=f"d-{uuid.uuid4().hex[:6]}")
    session.add_all([faces, cam])
    await session.flush()
    session.add(CameraDetector(camera_id=cam.id, detector_id=faces.id))
    await session.flush()

    assert await _names(session, cam) == {"faces"}
