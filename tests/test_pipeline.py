"""Pipeline completo: descarga (Flussonic simulado) -> ffmpeg real -> servicio de personas simulado -> BD real.

Se omite si no hay ffmpeg o PostgreSQL.
"""
import json
import shutil
import subprocess
import uuid
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import Alert, Camera, CameraDetector, Clip, ClipStatus, Detection, Detector, DetectorAccess
from app.detectors.registry import DetectorPool, detectors_for_camera
from app.ingest.flussonic import FlussonicClient
from app.pipeline.runner import process_clip

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg no disponible")

FALLEN = {
    "service": "person_detection", "detected": True, "persons_count": 1, "has_vulnerable_person": True,
    "last_incident_evidence": {"filename": "evidencia.mp4"},
    "detections": [{
        "track_id": 3, "confidence": 0.9, "alert": "critical_unresponsive_person_on_ground",
        "bbox": {"x1": 1, "y1": 2, "x2": 3, "y2": 4},
    }],
}


class FakeEvents:
    def __init__(self):
        self.published: list[str] = []

    async def publish(self, message: str) -> None:
        self.published.append(message)


@pytest.fixture
def video_bytes(tmp_path):
    out = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=160x120:rate=10",
         "-pix_fmt", "yuv420p", str(out)],
        check=True,
    )
    return out.read_bytes()


@respx.mock
async def test_clip_flows_from_flussonic_to_stored_detections(session, tmp_path, video_bytes):
    user = uuid.uuid4()
    camera = Camera(name="Serbot", stream_name="SERBOT01", robot_code="SERBOT01", owner_user_id=user)
    detector = Detector(
        name=f"personas-{uuid.uuid4().hex[:6]}", kind="persons", base_url="http://person:8001",
        adapter="person_detection_v1", min_confidence=0.5,
    )
    session.add_all([camera, detector])
    await session.flush()
    session.add_all([
        DetectorAccess(detector_id=detector.id, user_id=user),
        CameraDetector(camera_id=camera.id, detector_id=detector.id),
    ])
    started = datetime(2026, 1, 1, tzinfo=UTC)
    clip = Clip(camera_id=camera.id, started_at=started, duration_s=3)
    session.add(clip)
    await session.flush()
    clip.camera = camera

    respx.get("http://dvr.test/SERBOT01/archive-1767225600-3.mp4").mock(
        return_value=httpx.Response(200, content=video_bytes)
    )
    person = respx.post("http://person:8001/api/v1/analyze/frame").mock(return_value=httpx.Response(200, json=FALLEN))

    settings = Settings(flussonic_url="http://dvr.test", clips_dir=tmp_path / "clips", frame_sample_fps=1.0)
    events, pool = FakeEvents(), DetectorPool()
    rows = await detectors_for_camera(session, camera)
    await process_clip(
        session, clip, settings, FlussonicClient(settings), [pool.get(r) for r in rows], events
    )
    await pool.aclose()

    assert clip.status == ClipStatus.DONE, clip.error
    saved = (await session.execute(select(Detection).where(Detection.clip_id == clip.id))).scalars().all()
    assert len(saved) == 3  # 3 s a 1 fps
    assert {d.alert for d in saved} == {"critical_unresponsive_person_on_ground"}
    assert saved[0].bbox == [1, 2, 3, 4] and saved[0].track_id == 3
    assert saved[0].raw["incident_evidence"]["filename"] == "evidencia.mp4"
    # Misma persona y misma alerta (conf 0.9) en 3 frames seguidos -> confirmada
    assert all(d.alert_confirmed for d in saved)
    # evidencia conservada
    assert clip.path and (tmp_path / "clips" / f"clip_{clip.id}.mp4").exists()
    events = [json.loads(m) for m in events.published]
    assert [e["type"] for e in events].count("detection") == 3
    (stored,) = (await session.execute(select(Alert).where(Alert.clip_id == clip.id))).scalars().all()
    assert (stored.alert, stored.track_id, stored.frames, stored.max_confidence) == (
        "critical_unresponsive_person_on_ground", 3, 3, 0.9,
    )
    assert stored.camera_id == camera.id and stored.acknowledged_at is None
    assert stored.started_at == started  # el primer frame del clip es el segundo 0
    (confirmed,) = [e for e in events if e["type"] == "alert_confirmed"]
    assert confirmed["alert_id"] == str(stored.id)
    assert (confirmed["alert"], confirmed["track_id"], confirmed["frames"]) == (
        "critical_unresponsive_person_on_ground", 3, 3,
    )

    # Cada frame viaja con la camara y su instante real (inicio del clip + offset)
    stamps = [float(_field(c.request.content, "timestamp")) for c in person.calls]
    assert [_field(c.request.content, "robot_id") for c in person.calls] == ["SERBOT01"] * 3
    assert stamps == [started.timestamp() + i for i in range(3)]


@respx.mock
async def test_failing_service_does_not_break_the_clip(session, tmp_path, video_bytes):
    camera = Camera(name="Serbot", stream_name="SERBOT02")
    detector = Detector(name=f"p-{uuid.uuid4().hex[:6]}", kind="persons", base_url="http://person:8001",
                        adapter="person_detection_v1")
    session.add_all([camera, detector])
    await session.flush()
    session.add(CameraDetector(camera_id=camera.id, detector_id=detector.id))
    clip = Clip(camera_id=camera.id, started_at=datetime(2026, 1, 1, tzinfo=UTC), duration_s=3)
    session.add(clip)
    await session.flush()
    clip.camera = camera

    respx.get("http://dvr.test/SERBOT02/archive-1767225600-3.mp4").mock(
        return_value=httpx.Response(200, content=video_bytes)
    )
    respx.post("http://person:8001/api/v1/analyze/frame").mock(return_value=httpx.Response(500))

    settings = Settings(flussonic_url="http://dvr.test", clips_dir=tmp_path / "clips")
    pool = DetectorPool()
    rows = await detectors_for_camera(session, camera)
    await process_clip(session, clip, settings, FlussonicClient(settings), [pool.get(r) for r in rows], FakeEvents())
    await pool.aclose()

    assert clip.status == ClipStatus.DONE
    assert (await session.execute(select(Detection).where(Detection.clip_id == clip.id))).first() is None
    # sin alertas confirmadas, el video se borra
    assert clip.path is None and not (tmp_path / "clips" / f"clip_{clip.id}.mp4").exists()


def _field(multipart: bytes, name: str) -> str:
    marker = f'name="{name}"'.encode()
    after = multipart.split(marker, 1)[1]
    return after.split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0].decode()


@respx.mock
async def test_low_confidence_alert_is_stored_but_not_confirmed(session, tmp_path, video_bytes):
    camera = Camera(name="Serbot", stream_name="SERBOT03")
    detector = Detector(name=f"p-{uuid.uuid4().hex[:6]}", kind="persons", base_url="http://person:8001",
                        adapter="person_detection_v1")
    session.add_all([camera, detector])
    await session.flush()
    session.add(CameraDetector(camera_id=camera.id, detector_id=detector.id))
    clip = Clip(camera_id=camera.id, started_at=datetime(2026, 1, 1, tzinfo=UTC), duration_s=3)
    session.add(clip)
    await session.flush()
    clip.camera = camera

    weak = json.loads(json.dumps(FALLEN))
    weak["detections"][0]["confidence"] = 0.55  # bajo el umbral de alerta (0.7), sobre el de deteccion
    respx.get("http://dvr.test/SERBOT03/archive-1767225600-3.mp4").mock(
        return_value=httpx.Response(200, content=video_bytes)
    )
    respx.post("http://person:8001/api/v1/analyze/frame").mock(return_value=httpx.Response(200, json=weak))

    settings = Settings(flussonic_url="http://dvr.test", clips_dir=tmp_path / "clips")
    events, pool = FakeEvents(), DetectorPool()
    rows = await detectors_for_camera(session, camera)
    await process_clip(session, clip, settings, FlussonicClient(settings), [pool.get(r) for r in rows], events)
    await pool.aclose()

    saved = (await session.execute(select(Detection).where(Detection.clip_id == clip.id))).scalars().all()
    assert len(saved) == 3 and all(d.alert for d in saved)  # la alerta cruda se conserva
    assert not any(d.alert_confirmed for d in saved)
    assert "alert_confirmed" not in [json.loads(m)["type"] for m in events.published]


async def _pending_clip(session, stream: str) -> Clip:
    camera = Camera(name="Serbot", stream_name=stream)
    detector = Detector(name=f"p-{uuid.uuid4().hex[:6]}", kind="persons", base_url="http://person:8001",
                        adapter="person_detection_v1")
    session.add_all([camera, detector])
    await session.flush()
    session.add(CameraDetector(camera_id=camera.id, detector_id=detector.id))
    clip = Clip(camera_id=camera.id, started_at=datetime(2026, 1, 1, tzinfo=UTC), duration_s=3)
    session.add(clip)
    await session.flush()
    clip.camera = camera
    return clip


@respx.mock
async def test_window_without_archive_is_no_data_not_a_failure(session, tmp_path):
    clip = await _pending_clip(session, "SERBOT04")
    respx.get("http://dvr.test/SERBOT04/archive-1767225600-3.mp4").mock(
        return_value=httpx.Response(400, text="No DVR configured")
    )
    person = respx.post("http://person:8001/api/v1/analyze/frame").mock(return_value=httpx.Response(200, json=FALLEN))

    settings = Settings(flussonic_url="http://dvr.test", clips_dir=tmp_path / "clips")
    pool, events = DetectorPool(), FakeEvents()
    rows = await detectors_for_camera(session, clip.camera)
    await process_clip(session, clip, settings, FlussonicClient(settings), [pool.get(r) for r in rows], events)
    await pool.aclose()

    assert clip.status == ClipStatus.NO_DATA
    assert "Sin archivo" in clip.error
    assert not person.called and events.published == []


@respx.mock
async def test_keep_clips_setting_preserves_every_video(session, tmp_path, video_bytes):
    clip = await _pending_clip(session, "SERBOT05")
    respx.get("http://dvr.test/SERBOT05/archive-1767225600-3.mp4").mock(
        return_value=httpx.Response(200, content=video_bytes)
    )
    respx.post("http://person:8001/api/v1/analyze/frame").mock(
        return_value=httpx.Response(200, json={"detected": False})
    )

    settings = Settings(flussonic_url="http://dvr.test", clips_dir=tmp_path / "clips", keep_clips=True)
    pool = DetectorPool()
    rows = await detectors_for_camera(session, clip.camera)
    await process_clip(session, clip, settings, FlussonicClient(settings), [pool.get(r) for r in rows], FakeEvents())
    await pool.aclose()

    assert clip.status == ClipStatus.DONE
    assert clip.path and (tmp_path / "clips" / f"clip_{clip.id}.mp4").exists()


class SpyEvents(FakeEvents):
    """Anota, al publicar cada evento, cuantos objetos seguian sin guardarse en la sesion."""

    def __init__(self, session):
        super().__init__()
        self._session = session
        self.unsaved_when_published: list[int] = []

    async def publish(self, message: str) -> None:
        self.unsaved_when_published.append(len(self._session.new) + len(self._session.dirty))
        await super().publish(message)


@respx.mock
async def test_events_are_published_only_after_their_data_is_saved(session, tmp_path, video_bytes):
    clip = await _pending_clip(session, "SERBOT06")
    respx.get("http://dvr.test/SERBOT06/archive-1767225600-3.mp4").mock(
        return_value=httpx.Response(200, content=video_bytes)
    )
    respx.post("http://person:8001/api/v1/analyze/frame").mock(return_value=httpx.Response(200, json=FALLEN))

    settings = Settings(flussonic_url="http://dvr.test", clips_dir=tmp_path / "clips")
    events, pool = SpyEvents(session), DetectorPool()
    rows = await detectors_for_camera(session, clip.camera)
    await process_clip(session, clip, settings, FlussonicClient(settings), [pool.get(r) for r in rows], events)
    await pool.aclose()

    assert len(events.published) == 4  # 3 detecciones y 1 alerta confirmada
    assert events.unsaved_when_published == [0, 0, 0, 0]  # cuando salio cada evento, todo estaba guardado


@respx.mock
async def test_reprocessing_a_clip_replaces_what_a_dead_worker_left_behind(session, tmp_path, video_bytes):
    clip = await _pending_clip(session, "SERBOT07")
    clip.attempts = 2  # un worker lo tomo antes, se cayo, y ahora es el segundo intento
    detector = (await detectors_for_camera(session, clip.camera))[0]
    session.add(Detection(clip_id=clip.id, detector_id=detector.id, label="person", confidence=0.9, offset_s=0))
    session.add(Alert(
        clip_id=clip.id, camera_id=clip.camera_id, detector_id=detector.id, alert="old",
        started_at=clip.started_at, first_offset_s=0, offset_s=2, frames=3, max_confidence=0.9,
    ))
    await session.flush()
    respx.get("http://dvr.test/SERBOT07/archive-1767225600-3.mp4").mock(
        return_value=httpx.Response(200, content=video_bytes)
    )
    respx.post("http://person:8001/api/v1/analyze/frame").mock(
        return_value=httpx.Response(200, json={"detected": False})
    )

    settings = Settings(flussonic_url="http://dvr.test", clips_dir=tmp_path / "clips")
    pool = DetectorPool()
    rows = await detectors_for_camera(session, clip.camera)
    await process_clip(session, clip, settings, FlussonicClient(settings), [pool.get(r) for r in rows], FakeEvents())
    await pool.aclose()

    assert clip.status == ClipStatus.DONE
    assert (await session.execute(select(Detection).where(Detection.clip_id == clip.id))).first() is None
    assert (await session.execute(select(Alert).where(Alert.clip_id == clip.id))).first() is None
