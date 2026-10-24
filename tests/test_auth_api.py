"""Autenticacion con robotics-backend (simulado) y visibilidad: cada usuario ve solo sus robots.

Usa el Postgres de DATABASE_URL dentro de una transaccion que se revierte.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.auth.access import CameraAccess
from app.auth.backend import CurrentUser
from app.db.models import AuditLog, Clip, ClipStatus, Detection, Detector
from tests.helpers import ADMIN_ID, USER_A, USER_B, bearer, make_camera


async def names(api, path: str, token: str) -> set[str]:
    resp = await api.get(path, headers=bearer(token))
    assert resp.status_code == 200, resp.text
    return {c["name"] for c in resp.json()}


async def test_requests_without_a_valid_token_are_rejected(api):
    assert (await api.get("/cameras")).status_code == 401
    assert (await api.get("/cameras", headers=bearer("token-falso"))).status_code == 401
    assert (await api.get("/health")).status_code == 200  # el health check sigue publico


async def test_auth_is_unavailable_when_robotics_backend_is_down(api):
    api.state["down"] = True
    assert (await api.get("/cameras", headers=bearer("tok-a"))).status_code == 503


async def test_validated_token_is_cached(api):
    for _ in range(3):
        assert (await api.get("/cameras", headers=bearer("tok-a"))).status_code == 200
    assert api.state["calls"] == 1


async def test_each_user_only_sees_their_own_cameras_and_admin_sees_all(api, session):
    cam_a = await make_camera(session, USER_A, "ana")
    cam_b = await make_camera(session, USER_B, "beto")

    assert await names(api, "/cameras", "tok-a") == {cam_a.name}
    assert await names(api, "/cameras", "tok-b") == {cam_b.name}
    assert {cam_a.name, cam_b.name} <= await names(api, "/cameras", "tok-admin")


async def test_only_admin_creates_cameras_and_assigns_the_owner(api, session):
    body = {"name": "SERBOT01", "stream_name": f"SERBOT01-{uuid.uuid4().hex[:6]}", "owner_user_id": str(USER_A)}

    assert (await api.post("/cameras", json=body, headers=bearer("tok-a"))).status_code == 403

    resp = await api.post("/cameras", json=body, headers=bearer("tok-admin"))
    assert resp.status_code == 201
    assert "SERBOT01" in await names(api, "/cameras", "tok-a")  # la ve su dueño
    assert "SERBOT01" not in await names(api, "/cameras", "tok-b")

    audit = (await session.execute(select(AuditLog).where(AuditLog.action == "camera.create"))).scalars().all()
    assert [a.actor_id for a in audit] == [str(ADMIN_ID)]


async def test_clips_can_only_be_requested_on_own_cameras(api, session):
    cam_a = await make_camera(session, USER_A, "ana")
    body = {"camera_id": str(cam_a.id), "started_at": "2026-01-01T00:00:00Z", "duration_s": 10}

    resp = await api.post("/clips", json=body, headers=bearer("tok-a"))
    assert resp.status_code == 202
    assert resp.json()["requested_by"] == str(USER_A)
    queued = await session.get(Clip, uuid.UUID(resp.json()["id"]))
    assert queued.status == ClipStatus.PENDING  # el propio clip es el trabajo en cola

    # El otro usuario no puede ni saber que esa camara existe
    assert (await api.post("/clips", json=body, headers=bearer("tok-b"))).status_code == 404
    assert len((await session.execute(select(Clip).where(Clip.camera_id == cam_a.id))).scalars().all()) == 1

    audit = (await session.execute(select(AuditLog).where(AuditLog.action == "clip.request"))).scalars().all()
    assert [a.actor_id for a in audit] == [str(USER_A)]


async def test_clips_and_detections_are_isolated_per_user(api, session):
    cam_a = await make_camera(session, USER_A, "ana")
    detector = Detector(name=f"p-{uuid.uuid4().hex[:6]}", kind="persons", base_url="http://p:8001")
    clip = Clip(camera_id=cam_a.id, started_at=datetime(2026, 1, 1, tzinfo=UTC), duration_s=10)
    session.add_all([detector, clip])
    await session.flush()
    session.add(Detection(clip_id=clip.id, detector_id=detector.id, label="person", confidence=0.9, offset_s=0))
    await session.flush()

    for path, ok_status in ((f"/clips/{clip.id}", 200),):
        assert (await api.get(path, headers=bearer("tok-a"))).status_code == ok_status
        assert (await api.get(path, headers=bearer("tok-b"))).status_code == 404
        assert (await api.get(path, headers=bearer("tok-admin"))).status_code == 200

    def clip_ids(resp):
        return {c["id"] for c in resp.json()}

    assert str(clip.id) in clip_ids(await api.get("/clips", headers=bearer("tok-a")))
    assert clip_ids(await api.get("/clips", headers=bearer("tok-b"))) == set()

    assert len((await api.get("/detections", headers=bearer("tok-a"))).json()) == 1
    assert (await api.get("/detections", headers=bearer("tok-b"))).json() == []
    assert len((await api.get("/detections", headers=bearer("tok-admin"))).json()) >= 1


async def test_websocket_access_follows_camera_ownership():
    cam_a, cam_b = uuid.uuid4(), uuid.uuid4()
    owners = {cam_a: USER_A, cam_b: USER_B}
    lookups = []

    async def owner_of(camera_id):
        lookups.append(camera_id)
        return owners.get(camera_id)

    ana = CameraAccess(CurrentUser(USER_A, "ana", "operator"), owner_of)
    assert await ana.allows(str(cam_a)) is True
    assert await ana.allows(str(cam_b)) is False
    assert await ana.allows(str(uuid.uuid4())) is False  # camara desconocida
    await ana.allows(str(cam_a))
    assert len(lookups) == 3  # la respuesta repetida sale de la cache

    admin = CameraAccess(CurrentUser(ADMIN_ID, "admin", "admin"), owner_of)
    assert await admin.allows(str(cam_b)) is True
    assert len(lookups) == 3  # el admin no consulta
