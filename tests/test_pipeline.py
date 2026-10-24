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
from app.db.models import Camera, CameraDetector, Clip, ClipStatus, Detection, Detector, DetectorAccess
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


class FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


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
    redis, pool = FakeRedis(), DetectorPool()
    rows = await detectors_for_camera(session, camera)
    await process_clip(
        session, clip, settings, FlussonicClient(settings), [pool.get(r) for r in rows], redis
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
    events = [json.loads(m) for _, m in redis.published]
    assert [e["type"] for e in events].count("detection") == 3
    (confirmed,) = [e for e in events if e["type"] == "alert_confirmed"]
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
    await process_clip(session, clip, settings, FlussonicClient(settings), [pool.get(r) for r in rows], FakeRedis())
    await pool.aclose()

    assert clip.status == ClipStatus.DONE
    assert (await session.execute(select(Detection).where(Detection.clip_id == clip.id))).first() is None


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
    redis, pool = FakeRedis(), DetectorPool()
    rows = await detectors_for_camera(session, camera)
    await process_clip(session, clip, settings, FlussonicClient(settings), [pool.get(r) for r in rows], redis)
    await pool.aclose()

    saved = (await session.execute(select(Detection).where(Detection.clip_id == clip.id))).scalars().all()
    assert len(saved) == 3 and all(d.alert for d in saved)  # la alerta cruda se conserva
    assert not any(d.alert_confirmed for d in saved)
    assert "alert_confirmed" not in [json.loads(m)["type"] for _, m in redis.published]
