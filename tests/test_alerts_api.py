"""Registro de alertas confirmadas: listado por usuario, filtros y atencion."""
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Alert, AuditLog, Clip, Detector
from tests.helpers import ADMIN_ID, USER_A, USER_B, bearer, make_camera

A, B, ADMIN = bearer("tok-a"), bearer("tok-b"), bearer("tok-admin")
SOS, FALL = "person_requesting_help_sos", "critical_unresponsive_person_on_ground"
BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


async def make_alert(
    session, camera, *, minutes: int = 0, alert: str = SOS, acknowledged: bool = False, video: Path | None = None,
) -> Alert:
    clip = Clip(camera_id=camera.id, started_at=BASE + timedelta(minutes=minutes), duration_s=30,
                path=str(video) if video else None)
    detector = Detector(name=f"p-{uuid.uuid4().hex[:6]}", kind="persons", base_url="http://p:8001")
    session.add_all([clip, detector])
    await session.flush()
    row = Alert(
        clip_id=clip.id, camera_id=camera.id, detector_id=detector.id, alert=alert, track_id=3,
        started_at=clip.started_at + timedelta(seconds=4), first_offset_s=4, offset_s=6, frames=3,
        max_confidence=0.88,
        acknowledged_at=datetime.now(UTC) if acknowledged else None,
        acknowledged_by=USER_A if acknowledged else None,
    )
    session.add(row)
    await session.flush()
    return row


async def ids(api, path: str, headers: dict) -> list[str]:
    resp = await api.get(path, headers=headers)
    assert resp.status_code == 200, resp.text
    return [a["id"] for a in resp.json()]


async def test_each_user_only_sees_the_alerts_of_their_own_cameras(api, session):
    cam_a, cam_b = await make_camera(session, USER_A, "ana"), await make_camera(session, USER_B, "beto")
    mine, theirs = await make_alert(session, cam_a), await make_alert(session, cam_b)

    assert await ids(api, "/alerts", A) == [str(mine.id)]
    assert await ids(api, "/alerts", B) == [str(theirs.id)]
    assert {str(mine.id), str(theirs.id)} <= set(await ids(api, "/alerts", ADMIN))
    assert (await api.get("/alerts")).status_code == 401

    assert (await api.get(f"/alerts/{mine.id}", headers=A)).status_code == 200
    assert (await api.get(f"/alerts/{mine.id}", headers=B)).status_code == 404  # como si no existiera
    assert (await api.get(f"/alerts/{uuid.uuid4()}", headers=A)).status_code == 404


async def test_alert_carries_what_the_screen_needs_to_show_it(api, session):
    camera = await make_camera(session, USER_A, "ana")
    camera.robot_code = "SERBOT01"
    alert = await make_alert(session, camera)

    got = (await api.get(f"/alerts/{alert.id}", headers=A)).json()

    assert got["robot_code"] == "SERBOT01" and got["camera_id"] == str(camera.id)
    assert (got["alert"], got["track_id"], got["frames"], got["max_confidence"]) == (SOS, 3, 3, 0.88)
    assert got["started_at"].startswith("2026-01-01T12:00:04")  # inicio del clip + first_offset_s
    assert got["acknowledged_at"] is None and got["acknowledged_by"] is None


async def test_pending_alerts_can_be_listed_newest_first_and_filtered(api, session):
    camera, other = await make_camera(session, USER_A, "ana"), await make_camera(session, USER_A, "ana2")
    old = await make_alert(session, camera, minutes=0)
    done = await make_alert(session, camera, minutes=5, acknowledged=True)
    fall = await make_alert(session, other, minutes=10, alert=FALL)
    newest = await make_alert(session, camera, minutes=20)

    assert await ids(api, "/alerts", A) == [str(x.id) for x in (newest, fall, done, old)]  # la mas reciente primero
    assert await ids(api, "/alerts?acknowledged=false", A) == [str(x.id) for x in (newest, fall, old)]
    assert await ids(api, "/alerts?acknowledged=true", A) == [str(done.id)]
    assert await ids(api, f"/alerts?alert={FALL}", A) == [str(fall.id)]
    assert await ids(api, f"/alerts?camera_id={other.id}", A) == [str(fall.id)]
    assert await ids(api, "/alerts?since=2026-01-01T12:09:00Z", A) == [str(newest.id), str(fall.id)]
    assert await ids(api, "/alerts?until=2026-01-01T12:06:00Z", A) == [str(done.id), str(old.id)]
    assert await ids(api, "/alerts?limit=2", A) == [str(newest.id), str(fall.id)]
    assert await ids(api, "/alerts?limit=2&offset=2", A) == [str(done.id), str(old.id)]


async def test_acknowledging_records_who_when_and_tells_the_other_screens(api, session):
    camera = await make_camera(session, USER_A, "ana")
    alert = await make_alert(session, camera)

    resp = await api.post(f"/alerts/{alert.id}/acknowledge", json={"note": "Se envio a seguridad"}, headers=A)

    assert resp.status_code == 200
    got = resp.json()
    assert got["acknowledged_by"] == str(USER_A) and got["note"] == "Se envio a seguridad"
    assert got["acknowledged_at"] is not None
    assert await ids(api, "/alerts?acknowledged=false", A) == []

    (event,) = [json.loads(m) for m in api.publisher.published]
    assert event["type"] == "alert_acknowledged"
    assert (event["alert_id"], event["camera_id"]) == (str(alert.id), str(camera.id))

    audit = (await session.execute(select(AuditLog).where(AuditLog.action == "alert.acknowledge"))).scalars().all()
    assert [a.actor_id for a in audit] == [str(USER_A)]


async def test_acknowledging_twice_keeps_the_first_person_and_does_not_repeat_the_event(api, session):
    camera = await make_camera(session, USER_A, "ana")
    alert = await make_alert(session, camera)
    await api.post(f"/alerts/{alert.id}/acknowledge", json={"note": "primero"}, headers=A)

    again = await api.post(f"/alerts/{alert.id}/acknowledge", json={"note": "segundo"}, headers=ADMIN)

    assert again.status_code == 200
    assert again.json()["acknowledged_by"] == str(USER_A) and again.json()["note"] == "primero"
    assert len(api.publisher.published) == 1
    assert str(ADMIN_ID) not in again.text


async def test_nobody_acknowledges_someone_elses_alert_and_the_note_has_a_limit(api, session):
    alert = await make_alert(session, await make_camera(session, USER_A, "ana"))

    assert (await api.post(f"/alerts/{alert.id}/acknowledge", headers=B)).status_code == 404
    assert (await api.post(f"/alerts/{alert.id}/acknowledge")).status_code == 401
    assert (await api.post(f"/alerts/{alert.id}/acknowledge", json={"note": "x" * 501}, headers=A)).status_code == 422
    assert api.publisher.published == []
    assert (await api.get(f"/alerts/{alert.id}", headers=A)).json()["acknowledged_at"] is None

    assert (await api.post(f"/alerts/{alert.id}/acknowledge", headers=A)).status_code == 200  # la nota es opcional


VIDEO = b"ftypmp42" + bytes(range(256)) * 8  # no es un MP4 real: el endpoint solo entrega el archivo


def evidence(monkeypatch, tmp_path, name: str = "clip.mp4") -> Path:
    """Una carpeta de clips de prueba con un video dentro."""
    clips = tmp_path / "clips"
    clips.mkdir()
    monkeypatch.setenv("CLIPS_DIR", str(clips))
    get_settings.cache_clear()
    path = clips / name
    path.write_bytes(VIDEO)
    return path


async def test_the_alert_says_whether_its_video_is_still_stored(api, session, monkeypatch, tmp_path):
    camera = await make_camera(session, USER_A, "ana")
    with_video = await make_alert(session, camera, minutes=0, video=evidence(monkeypatch, tmp_path))
    without = await make_alert(session, camera, minutes=5)

    listed = {a["id"]: a["video_available"] for a in (await api.get("/alerts", headers=A)).json()}

    assert listed == {str(with_video.id): True, str(without.id): False}
    assert (await api.get(f"/alerts/{with_video.id}", headers=A)).json()["video_available"] is True
    acknowledged = await api.post(f"/alerts/{with_video.id}/acknowledge", headers=A)
    assert acknowledged.json()["video_available"] is True  # atender no cambia el video


async def test_the_owner_downloads_the_alert_video_and_nobody_else_does(api, session, monkeypatch, tmp_path):
    camera = await make_camera(session, USER_A, "ana")
    alert = await make_alert(session, camera, video=evidence(monkeypatch, tmp_path))

    mine = await api.get(f"/alerts/{alert.id}/video", headers=A)
    assert mine.status_code == 200
    assert mine.content == VIDEO and mine.headers["content-type"] == "video/mp4"
    assert "no-store" in mine.headers["cache-control"]
    assert (await api.get(f"/alerts/{alert.id}/video", headers=ADMIN)).content == VIDEO

    assert (await api.get(f"/alerts/{alert.id}/video", headers=B)).status_code == 404  # como si no existiera
    assert (await api.get(f"/alerts/{alert.id}/video")).status_code == 401
    assert (await api.get(f"/alerts/{alert.id}/video", headers=bearer("tok-op"))).status_code == 403
    assert (await api.get(f"/alerts/{uuid.uuid4()}/video", headers=A)).status_code == 404


async def test_the_player_can_seek_with_range_requests(api, session, monkeypatch, tmp_path):
    alert = await make_alert(session, await make_camera(session, USER_A, "ana"), video=evidence(monkeypatch, tmp_path))

    part = await api.get(f"/alerts/{alert.id}/video", headers={**A, "Range": "bytes=10-19"})

    assert part.status_code == 206 and part.content == VIDEO[10:20]
    assert part.headers["content-range"] == f"bytes 10-19/{len(VIDEO)}"


async def test_every_view_of_the_video_is_audited_and_denied_ones_are_not(api, session, monkeypatch, tmp_path):
    alert = await make_alert(session, await make_camera(session, USER_A, "ana"), video=evidence(monkeypatch, tmp_path))

    await api.get(f"/alerts/{alert.id}/video", headers=A)
    await api.get(f"/alerts/{alert.id}/video", headers=B)  # 404: no es suyo

    rows = (await session.execute(select(AuditLog).where(AuditLog.action == "alert.video"))).scalars().all()
    assert [(r.actor_id, r.entity_id) for r in rows] == [(str(USER_A), str(alert.id))]


async def test_a_video_that_is_gone_answers_with_a_clear_404(api, session, monkeypatch, tmp_path):
    camera = await make_camera(session, USER_A, "ana")
    path = evidence(monkeypatch, tmp_path)
    cleaned = await make_alert(session, camera, minutes=0)  # la limpieza periodica dejo path en None
    deleted = await make_alert(session, camera, minutes=5, video=path)
    path.unlink()  # el archivo desaparecio del disco aunque la base aun lo recuerde

    for alert in (cleaned, deleted):
        resp = await api.get(f"/alerts/{alert.id}/video", headers=A)
        assert resp.status_code == 404 and "ya no esta disponible" in resp.json()["detail"]
    rows = (await session.execute(select(AuditLog).where(AuditLog.action == "alert.video"))).scalars().all()
    assert rows == []


async def test_a_path_outside_the_clips_folder_is_never_served(api, session, monkeypatch, tmp_path):
    evidence(monkeypatch, tmp_path)
    secret = tmp_path / "secreto.mp4"
    secret.write_bytes(b"no deberia salir")
    camera = await make_camera(session, USER_A, "ana")
    outside = await make_alert(session, camera, minutes=0, video=secret)
    traversal = await make_alert(session, camera, minutes=5, video=tmp_path / "clips" / ".." / "secreto.mp4")

    for alert in (outside, traversal):
        resp = await api.get(f"/alerts/{alert.id}/video", headers=A)
        assert resp.status_code == 404 and b"deberia" not in resp.content
