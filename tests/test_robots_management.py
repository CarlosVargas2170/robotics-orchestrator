"""Gestion de robots y servicios: ver, buscar y eliminar robots, validar contra robotics-backend y vistas del admin."""
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db.models import AuditLog, Camera, CameraDetector, Clip
from tests.helpers import ADMIN_ID, INACTIVE_ID, OPERATOR_ID, USER_A, USER_B, bearer, make_camera

A, B, ADMIN = bearer("tok-a"), bearer("tok-b"), bearer("tok-admin")


async def new_camera(api, **fields):
    body = {"name": "Robot", "stream_name": f"s-{uuid.uuid4().hex[:8]}", **fields}
    return await api.post("/cameras", json=body, headers=ADMIN)


async def register(api, kind="persons"):
    body = {"name": f"{kind}-{uuid.uuid4().hex[:6]}", "kind": kind, "base_url": "http://svc:8001"}
    resp = await api.post("/detectors", json=body, headers=ADMIN)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def names(api, path, headers=ADMIN):
    resp = await api.get(path, headers=headers)
    assert resp.status_code == 200, resp.text
    return [c["name"] for c in resp.json()], resp.headers["x-total-count"]


# --- Ver, buscar y eliminar robots -----------------------------------------------------------


async def test_a_robot_can_be_viewed_by_its_owner_and_the_admin_only(api, session):
    camera = await make_camera(session, USER_A, "ana")

    assert (await api.get(f"/cameras/{camera.id}", headers=A)).json()["id"] == str(camera.id)
    assert (await api.get(f"/cameras/{camera.id}", headers=ADMIN)).status_code == 200
    assert (await api.get(f"/cameras/{camera.id}", headers=B)).status_code == 404  # como si no existiera
    assert (await api.get(f"/cameras/{uuid.uuid4()}", headers=A)).status_code == 404
    assert (await api.get(f"/cameras/{camera.id}")).status_code == 401


async def test_robots_can_be_searched_with_filters_pagination_and_total(api, session):
    ana1, ana2 = await make_camera(session, USER_A, "x"), await make_camera(session, USER_A, "x")
    beto = await make_camera(session, USER_B, "x")
    ana1.name, ana1.robot_code = "Mesero uno", "SERBOT01"
    ana2.name, ana2.enabled = "Mesero 100%", False
    beto.name, beto.stream_name = "Guardia", "patio-norte"
    await session.flush()
    only_mine = f"owner_user_id={USER_A}"

    # el admin ve los robots de un usuario
    assert await names(api, f"/cameras?{only_mine}") == (["Mesero 100%", "Mesero uno"], "2")
    assert await names(api, f"/cameras?{only_mine}&enabled=false") == (["Mesero 100%"], "1")
    assert await names(api, f"/cameras?{only_mine}&robot_code=SERBOT01") == (["Mesero uno"], "1")
    # la busqueda no distingue mayusculas y mira tambien el stream
    assert await names(api, f"/cameras?{only_mine}&q=mesero") == (["Mesero 100%", "Mesero uno"], "2")
    assert await names(api, "/cameras?q=patio-nor") == (["Guardia"], "1")
    # % es un caracter, no un comodin
    assert await names(api, f"/cameras?{only_mine}&q=%25") == (["Mesero 100%"], "1")
    # el total no depende de la pagina
    assert await names(api, f"/cameras?{only_mine}&limit=1&offset=1") == (["Mesero uno"], "2")

    # Un usuario normal solo ve lo suyo, aunque pida el dueño de otro
    assert (await names(api, "/cameras", A))[1] == "2"
    assert await names(api, f"/cameras?owner_user_id={USER_B}", A) == ([], "0")


async def test_a_robot_without_history_can_be_deleted_and_the_deletion_is_audited(api, session):
    camera = await make_camera(session, USER_A, "ana")
    detector = await register(api)
    session.add(CameraDetector(camera_id=camera.id, detector_id=uuid.UUID(detector["id"])))
    await session.flush()

    assert (await api.delete(f"/cameras/{camera.id}", headers=A)).status_code == 403
    assert (await api.delete(f"/cameras/{camera.id}", headers=ADMIN)).status_code == 204

    assert (await api.get(f"/cameras/{camera.id}", headers=ADMIN)).status_code == 404
    assert (await session.execute(select(CameraDetector).where(CameraDetector.camera_id == camera.id))).first() is None
    audit = (await session.execute(select(AuditLog).where(AuditLog.action == "camera.delete"))).scalars().all()
    assert [a.actor_id for a in audit] == [str(ADMIN_ID)]
    assert (await api.delete(f"/cameras/{camera.id}", headers=ADMIN)).status_code == 404


async def test_a_robot_with_history_is_kept_and_must_be_deactivated_instead(api, session):
    camera = await make_camera(session, USER_A, "ana")
    session.add(Clip(camera_id=camera.id, started_at=datetime(2026, 1, 1, tzinfo=UTC), duration_s=30))
    await session.flush()

    refused = await api.delete(f"/cameras/{camera.id}", headers=ADMIN)

    assert refused.status_code == 409 and "desactivalo" in refused.json()["detail"]
    assert (await api.get(f"/cameras/{camera.id}", headers=ADMIN)).status_code == 200
    off = await api.patch(f"/cameras/{camera.id}", json={"enabled": False}, headers=ADMIN)
    assert off.status_code == 200 and off.json()["enabled"] is False


# --- Validacion contra robotics-backend ------------------------------------------------------


async def test_a_robot_needs_an_owner_that_exists_in_robotics_backend(api, session):
    stream = f"s-{uuid.uuid4().hex[:8]}"

    ghost = await new_camera(api, owner_user_id=str(uuid.uuid4()), stream_name=stream)
    assert ghost.status_code == 422 and "no existe" in ghost.json()["detail"]
    inactive = await new_camera(api, owner_user_id=str(INACTIVE_ID), stream_name=stream)
    assert inactive.status_code == 422 and "desactivado" in inactive.json()["detail"]
    operator = await new_camera(api, owner_user_id=str(OPERATOR_ID), stream_name=stream)
    assert operator.status_code == 422 and "solo admin y cliente" in operator.json()["detail"]
    assert (await session.execute(select(Camera).where(Camera.stream_name == stream))).first() is None  # nada se guardo

    assert (await new_camera(api, owner_user_id=str(USER_A))).status_code == 201  # cliente
    assert (await new_camera(api, owner_user_id=str(ADMIN_ID))).status_code == 201  # admin


async def test_the_robot_code_must_exist_in_robotics_backend(api):
    assert (await new_camera(api, robot_code="SERBOT01")).status_code == 201
    assert (await new_camera(api, robot_code="VIEJO")).status_code == 201  # inactivo, pero existe
    unknown = await new_camera(api, robot_code="SERBOT99")
    assert unknown.status_code == 422 and "SERBOT99" in unknown.json()["detail"]


async def test_nothing_is_saved_when_robotics_backend_cannot_be_asked(api, session):
    api.state["directory_down"] = True
    stream = f"s-{uuid.uuid4().hex[:8]}"

    resp = await new_camera(api, owner_user_id=str(USER_A), stream_name=stream)

    assert resp.status_code == 503 and "robotics-backend" in resp.json()["detail"]
    assert (await session.execute(select(Camera).where(Camera.stream_name == stream))).first() is None
    assert (await new_camera(api, robot_code="SERBOT01")).status_code == 503


async def test_validation_asks_the_backend_with_the_admins_own_token_and_only_when_needed(api):
    await new_camera(api)  # sin dueño ni robot: no hay nada que validar
    assert api.state["directory"] == []

    await new_camera(api, owner_user_id=str(USER_A), robot_code="SERBOT02")
    assert {path for path, _ in api.state["directory"]} == {"/api/auth/users", "/api/robots"}
    assert {auth for _, auth in api.state["directory"]} == {"Bearer tok-admin"}  # sin credenciales aparte


async def test_reassigning_or_renaming_a_robot_validates_only_what_changes(api, session):
    camera = await make_camera(session, USER_A, "ana")
    url = f"/cameras/{camera.id}"

    assert (await api.patch(url, json={"owner_user_id": str(uuid.uuid4())}, headers=ADMIN)).status_code == 422
    assert (await api.patch(url, json={"owner_user_id": str(OPERATOR_ID)}, headers=ADMIN)).status_code == 422
    assert (await api.patch(url, json={"robot_code": "SERBOT99"}, headers=ADMIN)).status_code == 422
    assert camera.owner_user_id == USER_A and camera.robot_code is None  # los rechazos no cambiaron nada

    api.state["directory"].clear()
    assert (await api.patch(url, json={"location": "Piso 2"}, headers=ADMIN)).status_code == 200
    assert (await api.patch(url, json={"owner_user_id": str(USER_A)}, headers=ADMIN)).status_code == 200  # mismo dueño
    assert (await api.patch(url, json={"owner_user_id": None}, headers=ADMIN)).status_code == 200  # dejarlo sin dueño
    assert api.state["directory"] == []  # ninguna de las tres necesito preguntar al backend

    assert (await api.patch(url, json={"owner_user_id": str(USER_B), "robot_code": "SERBOT01"},
                            headers=ADMIN)).status_code == 200
    assert (camera.owner_user_id, camera.robot_code) == (USER_B, "SERBOT01")


async def test_a_service_permission_can_only_be_granted_to_a_real_product_user(api):
    detector = await register(api)
    path = f"/detectors/{detector['id']}/access"

    for bad in (uuid.uuid4(), INACTIVE_ID, OPERATOR_ID):
        assert (await api.put(f"{path}/{bad}", headers=ADMIN)).status_code == 422, bad
    assert (await api.get(path, headers=ADMIN)).json() == []  # no quedo ningun permiso fantasma
    assert (await api.put(f"{path}/{USER_A}", headers=ADMIN)).status_code == 200

    # Suspender o quitar no exige que el usuario exista: asi se limpia a quien ya fue borrado del backend
    ghost = uuid.uuid4()
    await api.put(f"{path}/{USER_B}", headers=ADMIN)
    assert (await api.put(f"{path}/{ghost}", json={"enabled": False}, headers=ADMIN)).status_code == 200
    assert (await api.delete(f"{path}/{ghost}", headers=ADMIN)).status_code == 204


# --- Catalogo de adaptadores y vista por usuario ---------------------------------------------


async def test_the_admin_can_list_which_kinds_of_service_the_system_understands(api):
    resp = await api.get("/detectors/adapters", headers=ADMIN)

    assert resp.status_code == 200
    adapters = {a["name"]: a for a in resp.json()}
    assert {"generic", "person_detection_v1"} <= set(adapters)
    person = adapters["person_detection_v1"]
    assert (person["analyze_path"], person["health_path"], person["has_health"]) == (
        "/api/v1/analyze/frame", "/api/v1/health", True)
    assert adapters["generic"]["has_health"] is False and adapters["generic"]["description"]
    assert [a["name"] for a in resp.json()] == sorted(adapters)
    assert (await api.get("/detectors/adapters", headers=A)).status_code == 403


async def test_the_admin_sees_every_service_a_user_has_and_where_it_is_on(api, session):
    persons, faces, objects, retired = [await register(api, k) for k in ("persons", "faces", "objects", "animals")]
    ana1, ana2 = await make_camera(session, USER_A, "uno"), await make_camera(session, USER_A, "dos")
    beto = await make_camera(session, USER_B, "beto")
    ana1.robot_code = "SERBOT01"
    for camera, detector, enabled in ((ana1, persons, True), (ana2, persons, False), (beto, persons, True)):
        session.add(CameraDetector(camera_id=camera.id, detector_id=uuid.UUID(detector["id"]), enabled=enabled))
    await session.flush()
    for detector, enabled in ((persons, True), (faces, False)):  # personas: permitido; rostros: suspendido
        await api.put(f"/detectors/{detector['id']}/access/{USER_A}", json={"enabled": enabled}, headers=ADMIN)
    await api.patch(f"/detectors/{retired['id']}", json={"enabled": False}, headers=ADMIN)

    resp = await api.get(f"/users/{USER_A}/detectors", headers=ADMIN)

    assert resp.status_code == 200
    by_kind = {d["kind"]: d for d in resp.json()}
    assert (by_kind["persons"]["allowed"], by_kind["persons"]["suspended"]) == (True, False)
    # solo el robot de ana donde esta encendido: ni ana2 (apagado) ni el de beto
    assert [(c["robot_code"], c["name"]) for c in by_kind["persons"]["active_on"]] == [("SERBOT01", ana1.name)]
    assert (by_kind["faces"]["allowed"], by_kind["faces"]["suspended"]) == (False, True)
    objects_view = by_kind["objects"]
    assert (objects_view["allowed"], objects_view["suspended"], objects_view["active_on"]) == (False, False, [])
    assert by_kind["animals"]["enabled"] is False  # deshabilitado globalmente
    assert objects["id"] in {d["detector_id"] for d in resp.json()}


async def test_the_per_user_view_works_for_unknown_users_and_is_admin_only(api):
    await register(api)

    resp = await api.get(f"/users/{uuid.uuid4()}/detectors", headers=ADMIN)  # p. ej. un usuario ya borrado del backend

    assert resp.status_code == 200 and resp.json() and not any(d["allowed"] for d in resp.json())
    assert (await api.get(f"/users/{USER_A}/detectors", headers=A)).status_code == 403
    assert (await api.get(f"/users/{USER_A}/detectors")).status_code == 401


@pytest.mark.parametrize("path", ["/detectors/adapters", f"/users/{uuid.uuid4()}/detectors"])
async def test_new_endpoints_are_closed_to_the_control_product_role(api, path):
    assert (await api.get(path, headers=bearer("tok-op"))).status_code == 403
