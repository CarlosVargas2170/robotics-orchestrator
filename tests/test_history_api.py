"""Historial: detecciones, clips y alertas con fechas, filtros, total y datos legibles; y el resumen."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import Alert, Clip, ClipStatus, Detection, Detector
from tests.helpers import USER_A, USER_B, bearer, make_camera

A, B, ADMIN = bearer("tok-a"), bearer("tok-b"), bearer("tok-admin")
BASE = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


async def make_detector(session, name="Personas", kind="persons") -> Detector:
    detector = Detector(name=f"{name}-{uuid.uuid4().hex[:5]}", kind=kind, base_url="http://svc:8001")
    session.add(detector)
    await session.flush()
    return detector


async def make_clip(session, camera, started: datetime, status=ClipStatus.DONE) -> Clip:
    clip = Clip(camera_id=camera.id, started_at=started, duration_s=30, status=status)
    session.add(clip)
    await session.flush()
    return clip


async def add_detections(session, clip, detector, offsets, *, label="person", alert=None, confirmed=False):
    rows = [Detection(clip_id=clip.id, detector_id=detector.id, label=label, confidence=0.9, offset_s=o,
                      alert=alert, alert_confirmed=confirmed) for o in offsets]
    session.add_all(rows)
    await session.flush()
    return rows


async def get(api, path, headers):
    resp = await api.get(path, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp


# --- Detecciones -----------------------------------------------------------------------------


async def test_detections_show_the_service_the_robot_and_the_real_moment(api, session):
    camera = await make_camera(session, USER_A, "ana")
    camera.robot_code = "SERBOT01"
    detector = await make_detector(session, "Personas", "persons")
    clip = await make_clip(session, camera, BASE)
    await add_detections(session, clip, detector, [12.5])

    (row,) = (await get(api, "/detections", A)).json()

    assert (row["robot_code"], row["detector_kind"], row["camera_id"]) == ("SERBOT01", "persons", str(camera.id))
    assert row["detector_name"].startswith("Personas-")
    assert datetime.fromisoformat(row["detected_at"]) == BASE + timedelta(seconds=12.5)  # inicio del clip + offset


async def test_detections_are_filtered_by_real_time_and_listed_newest_first(api, session):
    camera, detector = await make_camera(session, USER_A, "ana"), await make_detector(session)
    first = await make_clip(session, camera, BASE)
    second = await make_clip(session, camera, BASE + timedelta(minutes=10))
    await add_detections(session, first, detector, [5, 20])
    await add_detections(session, second, detector, [3])

    def offsets_of(resp):
        return [datetime.fromisoformat(d["detected_at"]) for d in resp.json()]

    everything = offsets_of(await get(api, "/detections", A))
    assert everything == sorted(everything, reverse=True) and len(everything) == 3

    start, end = iso(BASE + timedelta(seconds=10)), iso(BASE + timedelta(seconds=30))
    window = await get(api, f"/detections?since={start}&until={end}", A)
    assert offsets_of(window) == [BASE + timedelta(seconds=20)]  # solo la del segundo 20
    later = await get(api, f"/detections?since={iso(BASE + timedelta(minutes=5))}", A)
    assert later.json()[0]["clip_id"] == str(second.id)
    # una fecha sin zona horaria se toma como UTC
    assert (await get(api, "/detections?since=2026-03-10T12:00:10", A)).status_code == 200


async def test_detections_filter_by_service_type_robot_and_alert(api, session):
    cam1, cam2 = await make_camera(session, USER_A, "uno"), await make_camera(session, USER_A, "dos")
    persons, faces = await make_detector(session, "P", "persons"), await make_detector(session, "F", "faces")
    clip1, clip2 = await make_clip(session, cam1, BASE), await make_clip(session, cam2, BASE)
    await add_detections(session, clip1, persons, [1, 2])
    await add_detections(session, clip1, faces, [3], label="face")
    await add_detections(session, clip2, persons, [4], alert="person_requesting_help_sos", confirmed=True)

    async def count(query):
        resp = await get(api, f"/detections?{query}", A)
        return len(resp.json()), int(resp.headers["x-total-count"])

    assert await count("kind=persons") == (3, 3)
    assert await count("kind=faces") == (1, 1)
    assert await count(f"camera_id={cam2.id}") == (1, 1)
    assert await count("label=face") == (1, 1)
    assert await count("alert=person_requesting_help_sos") == (1, 1)
    assert await count("alert_confirmed=true") == (1, 1)
    assert await count("kind=persons&camera_id=" + str(cam1.id)) == (2, 2)
    assert await count("kind=nada") == (0, 0)


async def test_pagination_returns_the_page_but_the_total_of_everything(api, session):
    camera, detector = await make_camera(session, USER_A, "ana"), await make_detector(session)
    await add_detections(session, await make_clip(session, camera, BASE), detector, range(7))

    first = await get(api, "/detections?limit=3", A)
    second = await get(api, "/detections?limit=3&offset=3", A)
    last = await get(api, "/detections?limit=3&offset=6", A)

    assert [len(r.json()) for r in (first, second, last)] == [3, 3, 1]
    assert {r.headers["x-total-count"] for r in (first, second, last)} == {"7"}  # el total no depende de la pagina
    ids = [d["id"] for r in (first, second, last) for d in r.json()]
    assert len(set(ids)) == 7  # sin repetidos entre paginas
    assert len((await get(api, "/detections?limit=0", A)).json()) == 1  # el limite minimo es 1


async def test_a_user_only_sees_and_counts_their_own_history(api, session):
    cam_a, cam_b = await make_camera(session, USER_A, "ana"), await make_camera(session, USER_B, "beto")
    detector = await make_detector(session)
    await add_detections(session, await make_clip(session, cam_a, BASE), detector, [1, 2])
    await add_detections(session, await make_clip(session, cam_b, BASE), detector, [1, 2, 3])

    for token, expected in ((A, "2"), (B, "3")):
        assert (await get(api, "/detections", token)).headers["x-total-count"] == expected
    assert int((await get(api, "/detections", ADMIN)).headers["x-total-count"]) >= 5
    assert (await get(api, f"/detections?camera_id={cam_b.id}", A)).headers["x-total-count"] == "0"  # ajena: nada
    assert (await api.get("/detections")).status_code == 401


# --- Clips -----------------------------------------------------------------------------------


async def test_clips_are_filtered_by_date_status_and_robot_with_their_total(api, session):
    cam1, cam2 = await make_camera(session, USER_A, "uno"), await make_camera(session, USER_A, "dos")
    cam1.robot_code = "SERBOT01"
    done = await make_clip(session, cam1, BASE)
    no_data = await make_clip(session, cam1, BASE + timedelta(hours=1), ClipStatus.NO_DATA)
    failed = await make_clip(session, cam2, BASE + timedelta(hours=2), ClipStatus.FAILED)

    everything = await get(api, "/clips", A)
    newest_first = [str(failed.id), str(no_data.id), str(done.id)]
    assert [c["id"] for c in everything.json()] == newest_first
    assert everything.headers["x-total-count"] == "3"
    assert everything.json()[2]["robot_code"] == "SERBOT01"

    by_status = await get(api, "/clips?status=no_data", A)
    assert [c["id"] for c in by_status.json()] == [str(no_data.id)] and by_status.headers["x-total-count"] == "1"
    assert [c["id"] for c in (await get(api, f"/clips?camera_id={cam2.id}", A)).json()] == [str(failed.id)]
    start, end = iso(BASE + timedelta(minutes=30)), iso(BASE + timedelta(hours=1, minutes=30))
    window = await get(api, f"/clips?since={start}&until={end}", A)
    assert [c["id"] for c in window.json()] == [str(no_data.id)]
    paged = await get(api, "/clips?limit=1&offset=1", A)
    assert [c["id"] for c in paged.json()] == [str(no_data.id)] and paged.headers["x-total-count"] == "3"
    assert (await api.get("/clips?status=inventado", headers=A)).status_code == 422


async def test_clips_of_other_users_are_neither_listed_nor_counted(api, session):
    await make_clip(session, await make_camera(session, USER_B, "beto"), BASE)

    resp = await get(api, "/clips", A)

    assert resp.json() == [] and resp.headers["x-total-count"] == "0"


# --- Alertas ---------------------------------------------------------------------------------


async def test_alerts_report_their_total_and_the_service_that_raised_them(api, session):
    camera = await make_camera(session, USER_A, "ana")
    persons, faces = await make_detector(session, "P", "persons"), await make_detector(session, "F", "faces")
    for minutes, detector in ((0, persons), (5, persons), (10, faces)):
        clip = await make_clip(session, camera, BASE + timedelta(minutes=minutes))
        session.add(Alert(clip_id=clip.id, camera_id=camera.id, detector_id=detector.id, alert="sos",
                          started_at=clip.started_at, first_offset_s=0, offset_s=2, frames=3, max_confidence=0.8))
    await session.flush()

    page = await get(api, "/alerts?limit=2", A)
    assert len(page.json()) == 2 and page.headers["x-total-count"] == "3"
    assert page.json()[0]["detector_kind"] == "faces" and page.json()[0]["detector_name"].startswith("F-")

    only_persons = await get(api, "/alerts?kind=persons", A)
    assert {a["detector_kind"] for a in only_persons.json()} == {"persons"}
    assert only_persons.headers["x-total-count"] == "2"


async def test_the_browser_is_allowed_to_read_the_total_header(api):
    resp = await api.get("/alerts", headers={**A, "Origin": "http://localhost:3000"})

    assert "x-total-count" in resp.headers["access-control-expose-headers"].lower()


# --- Resumen ---------------------------------------------------------------------------------


async def seed_summary(session):
    """Ana: 3 alertas (una atendida) y 4 detecciones, en dos dias y con un clip sin datos. Beto: 1 alerta."""
    cam_a, cam_b = await make_camera(session, USER_A, "ana"), await make_camera(session, USER_B, "beto")
    cam_a.robot_code = "SERBOT01"
    persons = await make_detector(session, "Personas", "persons")
    faces = await make_detector(session, "Rostros", "faces")
    clip1 = await make_clip(session, cam_a, BASE)  # 10 mar 12:00 UTC
    # 11 mar 02:00 UTC = 10 mar 22:00 en La Paz
    clip2 = await make_clip(session, cam_a, BASE + timedelta(hours=14))
    await make_clip(session, cam_a, BASE + timedelta(hours=15), ClipStatus.NO_DATA)
    clip_b = await make_clip(session, cam_b, BASE)
    await add_detections(session, clip1, persons, [1, 2])
    await add_detections(session, clip2, faces, [1], label="face")
    await add_detections(session, clip2, persons, [2])
    await add_detections(session, clip_b, persons, [1])

    def alert(clip, camera, detector, kind, started, acknowledged=False):
        session.add(Alert(
            clip_id=clip.id, camera_id=camera.id, detector_id=detector.id, alert=kind, started_at=started,
            first_offset_s=0, offset_s=2, frames=3, max_confidence=0.8,
            acknowledged_at=started if acknowledged else None, acknowledged_by=USER_A if acknowledged else None))

    alert(clip1, cam_a, persons, "sos", BASE)
    alert(clip1, cam_a, persons, "sos", BASE + timedelta(minutes=1), acknowledged=True)
    alert(clip2, cam_a, persons, "fall", BASE + timedelta(hours=14))
    alert(clip_b, cam_b, persons, "sos", BASE)
    await session.flush()
    return cam_a, cam_b


WINDOW = f"since={iso(BASE - timedelta(days=1))}&until={iso(BASE + timedelta(days=2))}"


async def test_summary_counts_alerts_detections_and_clips_of_the_user(api, session):
    cam_a, _ = await seed_summary(session)

    summary = (await get(api, f"/stats/summary?{WINDOW}", A)).json()

    assert summary["alerts"]["total"] == 3 and summary["alerts"]["pending"] == 2
    assert summary["alerts"]["by_type"] == [{"alert": "sos", "count": 2}, {"alert": "fall", "count": 1}]
    assert summary["alerts"]["by_robot"] == [
        {"camera_id": str(cam_a.id), "name": cam_a.name, "robot_code": "SERBOT01", "count": 3}]
    assert summary["detections"]["total"] == 4
    assert [(s["kind"], s["count"]) for s in summary["detections"]["by_service"]] == [("persons", 3), ("faces", 1)]
    assert summary["clips"]["total"] == 3
    assert summary["clips"]["by_status"] == {"pending": 0, "downloading": 0, "processing": 0, "done": 2,
                                             "failed": 0, "no_data": 1}


async def test_summary_groups_by_day_in_the_requested_time_zone(api, session):
    await seed_summary(session)

    utc = (await get(api, f"/stats/summary?{WINDOW}", A)).json()
    bolivia = (await get(api, f"/stats/summary?{WINDOW}&tz=America/La_Paz", A)).json()

    # La alerta de las 02:00 UTC del 11 es del 10 por la noche en Bolivia (UTC-4)
    assert utc["alerts"]["by_day"] == [{"day": "2026-03-10", "count": 2}, {"day": "2026-03-11", "count": 1}]
    assert bolivia["alerts"]["by_day"] == [{"day": "2026-03-10", "count": 3}]
    assert bolivia["detections"]["by_day"] == [{"day": "2026-03-10", "count": 4}]
    assert bolivia["tz"] == "America/La_Paz"


async def test_summary_is_scoped_to_the_user_and_can_be_narrowed_to_one_robot(api, session):
    cam_a, cam_b = await seed_summary(session)

    beto = (await get(api, f"/stats/summary?{WINDOW}", B)).json()
    assert (beto["alerts"]["total"], beto["detections"]["total"], beto["clips"]["total"]) == (1, 1, 1)

    admin_all = (await get(api, f"/stats/summary?{WINDOW}", ADMIN)).json()
    assert admin_all["alerts"]["total"] >= 4
    admin_one = (await get(api, f"/stats/summary?{WINDOW}&camera_id={cam_b.id}", ADMIN)).json()
    assert (admin_one["alerts"]["total"], admin_one["detections"]["total"]) == (1, 1)

    ana_asking_for_beto = (await get(api, f"/stats/summary?{WINDOW}&camera_id={cam_b.id}", A)).json()
    assert ana_asking_for_beto["alerts"]["total"] == 0  # no puede espiar el robot de otro
    assert cam_a.id != cam_b.id


async def test_summary_defaults_to_the_last_seven_days_and_validates_its_input(api):
    default = (await get(api, "/stats/summary", A)).json()
    since, until = datetime.fromisoformat(default["since"]), datetime.fromisoformat(default["until"])
    assert until - since == timedelta(days=7)
    # la forma es estable aunque no haya datos
    assert default["alerts"]["total"] == 0 and default["clips"]["by_status"]["done"] == 0

    for bad in ("tz=Marte/Olimpo", "since=2026-03-10T00:00:00Z&until=2026-03-09T00:00:00Z",
                "since=2024-01-01T00:00:00Z&until=2026-01-01T00:00:00Z"):
        assert (await api.get(f"/stats/summary?{bad}", headers=A)).status_code == 422, bad
    assert (await api.get("/stats/summary")).status_code == 401
    assert (await api.get("/stats/summary", headers=bearer("tok-op"))).status_code == 403


@pytest.mark.parametrize("path", ["/detections", "/clips", "/alerts", "/stats/summary"])
async def test_history_is_closed_to_the_control_product_role(api, path):
    assert (await api.get(path, headers=bearer("tok-op"))).status_code == 403
