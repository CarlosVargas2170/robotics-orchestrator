"""Autenticacion con robotics-backend (simulado) y visibilidad: cada usuario ve solo sus robots.

Usa el Postgres de DATABASE_URL dentro de una transaccion que se revierte.
"""
import uuid
from datetime import UTC, datetime

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select

from app.api.deps import get_queue
from app.auth import backend
from app.auth.access import CameraAccess
from app.auth.backend import CurrentUser
from app.core.config import get_settings
from app.db.models import AuditLog, Camera, Clip, Detection, Detector
from app.db.session import get_session
from app.main import app

ADMIN_ID, USER_A, USER_B = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
BACKEND_USERS = {
    "tok-admin": {"id": str(ADMIN_ID), "login": "admin", "role": "admin", "active": True},
    "tok-a": {"id": str(USER_A), "login": "ana", "role": "operator", "active": True},
    "tok-b": {"id": str(USER_B), "login": "beto", "role": "operator", "active": True},
}


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class FakeQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, args))


@pytest.fixture
async def api(session, monkeypatch):
    monkeypatch.setenv("ROBOTICS_BACKEND_URL", "http://backend.test")
    get_settings.cache_clear()
    backend._client = None

    queue, state = FakeQueue(), {"down": False, "calls": 0}

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_queue] = lambda: queue

    def me(request):
        state["calls"] += 1
        if state["down"]:
            raise httpx.ConnectError("robotics-backend caido")
        user = BACKEND_USERS.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        return httpx.Response(200, json=user) if user else httpx.Response(401, json={"detail": "Unauthorized"})

    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://backend.test/api/auth/me").mock(side_effect=me)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://orq") as client:
            client.queue, client.state = queue, state
            yield client

    app.dependency_overrides.clear()
    await backend.close_auth_client()
    get_settings.cache_clear()


async def make_camera(session, owner: uuid.UUID, name: str) -> Camera:
    camera = Camera(name=f"{name}-{uuid.uuid4().hex[:6]}", stream_name=f"{name}-{uuid.uuid4().hex[:6]}",
                    owner_user_id=owner)
    session.add(camera)
    await session.flush()
    return camera


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
    assert api.queue.jobs == [("process_clip_job", (resp.json()["id"],))]

    # El otro usuario no puede ni saber que esa camara existe
    assert (await api.post("/clips", json=body, headers=bearer("tok-b"))).status_code == 404
    assert len(api.queue.jobs) == 1

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
