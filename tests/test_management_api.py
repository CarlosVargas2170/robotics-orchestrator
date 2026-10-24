"""Gestion de servicios de IA: registro, permisos por usuario y activacion por camara."""
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from app.db.models import AuditLog, Clip, Detection
from app.detectors.registry import detectors_for_camera
from app.detectors.validation import validate_service_url
from tests.helpers import ADMIN_ID, USER_A, USER_B, bearer, make_camera

ADMIN, A, B = bearer("tok-admin"), bearer("tok-a"), bearer("tok-b")


def new_detector(kind: str = "persons", **extra) -> dict:
    return {"name": f"{kind}-{uuid.uuid4().hex[:6]}", "kind": kind, "base_url": f"http://{kind}.svc:8001", **extra}


async def register(api, kind: str = "persons", **extra) -> dict:
    resp = await api.post("/detectors", json=new_detector(kind, **extra), headers=ADMIN)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def grant(api, detector: dict, user: uuid.UUID, enabled: bool = True) -> None:
    resp = await api.put(f"/detectors/{detector['id']}/access/{user}", json={"enabled": enabled}, headers=ADMIN)
    assert resp.status_code == 200, resp.text


# --- Registro de servicios -------------------------------------------------------------------


async def test_admin_registers_a_detector_with_defaults_and_it_is_audited(api, session):
    body = new_detector("persons", adapter="person_detection_v1", base_url="https://x.ngrok-free.dev/")
    resp = await api.post("/detectors", json=body, headers=ADMIN)

    assert resp.status_code == 201
    created = resp.json()
    assert created["base_url"] == "https://x.ngrok-free.dev"  # sin barra final
    assert (created["alert_min_confidence"], created["alert_min_frames"], created["max_concurrency"]) == (0.7, 3, 2)
    assert created["enabled"] is True

    audit = (await session.execute(select(AuditLog).where(AuditLog.action == "detector.create"))).scalars().all()
    assert [a.actor_id for a in audit] == [str(ADMIN_ID)]


async def test_only_admin_manages_detectors(api):
    detector = await register(api)
    for call in (
        api.post("/detectors", json=new_detector(), headers=A),
        api.get("/detectors", headers=A),
        api.get(f"/detectors/{detector['id']}", headers=A),
        api.patch(f"/detectors/{detector['id']}", json={"enabled": False}, headers=A),
        api.delete(f"/detectors/{detector['id']}", headers=A),
        api.get(f"/detectors/{detector['id']}/access", headers=A),
        api.put(f"/detectors/{detector['id']}/access/{USER_A}", headers=A),
    ):
        assert (await call).status_code == 403
    assert (await api.get("/detectors")).status_code == 401


@pytest.mark.parametrize("bad", [
    {"base_url": "ftp://svc:21"},
    {"base_url": "http://169.254.169.254/latest/meta-data"},
    {"base_url": "http://2852039166/"},  # 169.254.169.254 escrita como entero
    {"base_url": "http://user:pass@svc:8001"},
    {"adapter": "no-existe"},
    {"alert_min_confidence": 1.5},
    {"alert_min_frames": 0},
    {"max_concurrency": 0},
])
async def test_invalid_detector_is_rejected(api, bad):
    resp = await api.post("/detectors", json={**new_detector(), **bad}, headers=ADMIN)
    assert resp.status_code == 422


async def test_duplicate_detector_name_is_rejected(api):
    detector = await register(api)
    resp = await api.post("/detectors", json=new_detector(name=detector["name"]) | {"name": detector["name"]},
                          headers=ADMIN)
    assert resp.status_code == 409


async def test_patch_changes_only_the_given_fields(api):
    detector = await register(api, alert_min_frames=5)

    resp = await api.patch(f"/detectors/{detector['id']}", json={"enabled": False, "min_confidence": 0.4},
                           headers=ADMIN)

    assert resp.status_code == 200
    updated = resp.json()
    assert (updated["enabled"], updated["min_confidence"]) == (False, 0.4)
    assert (updated["alert_min_frames"], updated["base_url"]) == (5, detector["base_url"])  # intactos

    assert (await api.patch(f"/detectors/{detector['id']}", json={"base_url": None}, headers=ADMIN)).status_code == 422
    assert (await api.patch(f"/detectors/{uuid.uuid4()}", json={"enabled": True}, headers=ADMIN)).status_code == 404


async def test_detector_with_history_cannot_be_deleted_but_an_unused_one_can(api, session):
    used, unused = await register(api), await register(api)
    camera = await make_camera(session, None, "c")
    clip = Clip(camera_id=camera.id, started_at=datetime(2026, 1, 1, tzinfo=UTC), duration_s=10)
    session.add(clip)
    await session.flush()
    session.add(Detection(clip_id=clip.id, detector_id=uuid.UUID(used["id"]), label="person", confidence=0.9,
                          offset_s=0))
    await session.flush()

    assert (await api.delete(f"/detectors/{used['id']}", headers=ADMIN)).status_code == 409
    assert (await api.delete(f"/detectors/{unused['id']}", headers=ADMIN)).status_code == 204
    assert (await api.get(f"/detectors/{unused['id']}", headers=ADMIN)).status_code == 404


async def test_health_check_reports_the_service_state(api):
    online = await register(api, adapter="person_detection_v1", base_url="http://person.svc:8001")
    api.mock.get("http://person.svc:8001/api/v1/health").mock(
        return_value=httpx.Response(200, json={"service": "person_detection", "status": "ONLINE", "model_loaded": True})
    )
    resp = await api.get(f"/detectors/{online['id']}/health", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json()["online"] is True and resp.json()["detail"]["model_loaded"] is True

    offline = await register(api, adapter="person_detection_v1", base_url="http://caido.svc:8001")
    api.mock.get("http://caido.svc:8001/api/v1/health").mock(side_effect=httpx.ConnectError("sin conexion"))
    assert (await api.get(f"/detectors/{offline['id']}/health", headers=ADMIN)).json()["online"] is False

    generic = await register(api)  # el adaptador generico no define health check
    assert (await api.get(f"/detectors/{generic['id']}/health", headers=ADMIN)).status_code == 400


def test_service_url_validation():
    assert validate_service_url(" http://localhost:8001/ ") == "http://localhost:8001"
    assert validate_service_url("http://10.0.0.5:9000") == "http://10.0.0.5:9000"  # red interna permitida
    for bad in ("http://0.0.0.0", "http://[::ffff:169.254.169.254]/", "http://metadata.google.internal/", "file:///etc"):
        with pytest.raises(ValueError):
            validate_service_url(bad)


# --- Permisos por usuario --------------------------------------------------------------------


async def test_grant_list_suspend_and_revoke_access(api):
    detector = await register(api)
    await grant(api, detector, USER_A)

    listed = (await api.get(f"/detectors/{detector['id']}/access", headers=ADMIN)).json()
    assert listed == [{"user_id": str(USER_A), "enabled": True}]

    await grant(api, detector, USER_A, enabled=False)  # suspender
    assert (await api.get(f"/detectors/{detector['id']}/access", headers=ADMIN)).json()[0]["enabled"] is False

    assert (await api.delete(f"/detectors/{detector['id']}/access/{USER_A}", headers=ADMIN)).status_code == 204
    assert (await api.get(f"/detectors/{detector['id']}/access", headers=ADMIN)).json() == []
    assert (await api.delete(f"/detectors/{detector['id']}/access/{USER_A}", headers=ADMIN)).status_code == 404
    assert (await api.put(f"/detectors/{uuid.uuid4()}/access/{USER_A}", headers=ADMIN)).status_code == 404


async def test_user_only_sees_available_the_services_they_can_use(api):
    faces, animals, objects = [await register(api, kind) for kind in ("faces", "animals", "objects")]
    await register(api, "persons", enabled=False)
    await grant(api, faces, USER_A)
    await grant(api, animals, USER_A)
    await grant(api, objects, USER_A, enabled=False)  # permiso suspendido

    ana = (await api.get("/detectors/available", headers=A)).json()
    assert {d["name"] for d in ana} == {faces["name"], animals["name"]}
    assert all(set(d) == {"id", "name", "kind"} for d in ana)  # no se expone la URL interna
    assert (await api.get("/detectors/available", headers=B)).json() == []
    assert len((await api.get("/detectors/available", headers=ADMIN)).json()) >= 3


# --- Activacion por camara -------------------------------------------------------------------


async def test_user_activates_only_services_they_have_access_to_on_their_own_camera(api, session):
    animals, objects = await register(api, "animals"), await register(api, "objects")
    await grant(api, animals, USER_A)
    camera = await make_camera(session, USER_A, "ana")
    url = f"/cameras/{camera.id}/detectors"

    assert (await api.put(f"{url}/{animals['id']}", headers=A)).status_code == 200
    assert (await api.put(f"{url}/{objects['id']}", headers=A)).status_code == 403  # sin permiso
    assert (await api.put(f"{url}/{uuid.uuid4()}", headers=A)).status_code == 404

    active = (await api.get(url, headers=A)).json()
    assert [(d["name"], d["enabled"]) for d in active] == [(animals["name"], True)]

    assert (await api.put(f"{url}/{animals['id']}", json={"enabled": False}, headers=A)).status_code == 200
    assert (await api.get(url, headers=A)).json()[0]["enabled"] is False
    assert (await api.delete(f"{url}/{animals['id']}", headers=A)).status_code == 204
    assert (await api.get(url, headers=A)).json() == []


async def test_nobody_touches_the_services_of_someone_elses_camera(api, session):
    animals = await register(api, "animals")
    await grant(api, animals, USER_A)
    await grant(api, animals, USER_B)
    camera = await make_camera(session, USER_A, "ana")
    url = f"/cameras/{camera.id}/detectors/{animals['id']}"

    assert (await api.put(url, headers=B)).status_code == 404
    assert (await api.get(f"/cameras/{camera.id}/detectors", headers=B)).status_code == 404
    assert (await api.delete(url, headers=B)).status_code == 404


async def test_a_disabled_service_cannot_be_activated(api, session):
    detector = await register(api, "animals", enabled=False)
    await grant(api, detector, USER_A)
    camera = await make_camera(session, USER_A, "ana")

    assert (await api.put(f"/cameras/{camera.id}/detectors/{detector['id']}", headers=A)).status_code == 409


async def test_admin_activation_requires_the_owner_to_have_access(api, session):
    detector = await register(api, "faces")
    owned, ownerless = await make_camera(session, USER_A, "ana"), await make_camera(session, None, "libre")

    assert (await api.put(f"/cameras/{owned.id}/detectors/{detector['id']}", headers=ADMIN)).status_code == 409
    await grant(api, detector, USER_A)
    assert (await api.put(f"/cameras/{owned.id}/detectors/{detector['id']}", headers=ADMIN)).status_code == 200
    assert (await api.put(f"/cameras/{ownerless.id}/detectors/{detector['id']}", headers=ADMIN)).status_code == 200


async def test_admin_reassigns_a_camera_to_another_user(api, session):
    camera = await make_camera(session, USER_A, "ana")

    assert (await api.patch(f"/cameras/{camera.id}", json={"owner_user_id": str(USER_B)}, headers=A)).status_code == 403
    resp = await api.patch(f"/cameras/{camera.id}", json={"owner_user_id": str(USER_B), "location": "Piso 2"},
                           headers=ADMIN)

    assert resp.status_code == 200 and resp.json()["owner_user_id"] == str(USER_B)
    assert camera.name not in {c["name"] for c in (await api.get("/cameras", headers=A)).json()}
    assert camera.name in {c["name"] for c in (await api.get("/cameras", headers=B)).json()}
    assert (await api.patch(f"/cameras/{camera.id}", json={"name": None}, headers=ADMIN)).status_code == 422
    assert (await api.patch(f"/cameras/{uuid.uuid4()}", json={"enabled": False}, headers=ADMIN)).status_code == 404


# --- El escenario completo -------------------------------------------------------------------


async def test_two_users_with_different_services_get_different_routing(api, session):
    """Ana usa rostros y animales; Beto usa animales y objetos. Cada cual solo envia a los suyos."""
    faces, animals, objects = [await register(api, kind) for kind in ("faces", "animals", "objects")]
    for detector, users in ((faces, [USER_A]), (animals, [USER_A, USER_B]), (objects, [USER_B])):
        for user in users:
            await grant(api, detector, user)
    cam_a, cam_b = await make_camera(session, USER_A, "ana"), await make_camera(session, USER_B, "beto")

    for token, camera in ((A, cam_a), (B, cam_b)):
        for detector in (faces, animals, objects):
            resp = await api.put(f"/cameras/{camera.id}/detectors/{detector['id']}", headers=token)
            assert resp.status_code in (200, 403)  # 403 si no tiene ese servicio

    def kinds(rows):
        return {d.kind for d in rows}

    assert kinds(await detectors_for_camera(session, cam_a)) == {"faces", "animals"}
    assert kinds(await detectors_for_camera(session, cam_b)) == {"animals", "objects"}

    # Si el admin suspende un permiso, deja de enrutarse aunque la camara lo tenga activado
    await grant(api, animals, USER_A, enabled=False)
    assert kinds(await detectors_for_camera(session, cam_a)) == {"faces"}


async def test_admin_configures_automatic_ingestion_on_a_camera(api, session):
    camera = await make_camera(session, USER_A, "ana")
    cfg = {"auto_ingest": True, "ingest_interval_s": 60, "ingest_duration_s": 15}

    assert (await api.patch(f"/cameras/{camera.id}", json=cfg, headers=A)).status_code == 403
    resp = await api.patch(f"/cameras/{camera.id}", json=cfg, headers=ADMIN)
    assert resp.status_code == 200
    got = resp.json()
    assert (got["auto_ingest"], got["ingest_interval_s"], got["ingest_duration_s"]) == (True, 60, 15)

    # El intervalo no puede ser menor que la duracion (los tramos se solaparian)
    assert (await api.patch(f"/cameras/{camera.id}", json={"ingest_interval_s": 10}, headers=ADMIN)).status_code == 422
    assert (await api.patch(f"/cameras/{camera.id}", json={"ingest_duration_s": 90}, headers=ADMIN)).status_code == 422
    assert (await api.patch(f"/cameras/{camera.id}", json={"auto_ingest": None}, headers=ADMIN)).status_code == 422
    body = {"name": "x", "stream_name": "x", "ingest_interval_s": 10, "ingest_duration_s": 30}
    assert (await api.post("/cameras", json=body, headers=ADMIN)).status_code == 422
    assert (camera.ingest_interval_s, camera.ingest_duration_s) == (60, 15)  # los rechazos no cambiaron nada
