"""Ingesta automatica: ventanas de tiempo, programador contra la BD real, orden por camara y limpieza."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import CameraDetector, Clip, ClipSource, ClipStatus, Detector
from app.ingest.scheduler import ingest_cameras, next_window
from app.pipeline.housekeeping import cleanup_old_clips
from tests.helpers import make_camera, make_clip

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
WINDOW = dict(lag_s=20, max_backlog_s=300)


def at(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 1, 1, 11, minute, second, tzinfo=UTC)


# --- next_window: que tramo toca pedir -------------------------------------------------------


def test_first_run_asks_only_for_the_latest_complete_window():
    # Ahora 12:00:00, el archivo se da por cerrado con 20 s de antiguedad y el tramo dura 30 s -> 11:59:10
    assert next_window(None, NOW, interval_s=30, duration_s=30, **WINDOW) == at(59, 10)


def test_continuous_coverage_chains_windows_back_to_back():
    assert next_window(at(58, 40), NOW, interval_s=30, duration_s=30, **WINDOW) == at(59, 10)
    # el siguiente tramo todavia no esta cerrado
    assert next_window(at(59, 10), NOW, interval_s=30, duration_s=30, **WINDOW) is None


def test_sampling_skips_the_time_between_windows():
    # 15 s de cada minuto: la ultima ventana completa empieza en 11:59:25
    args = dict(interval_s=60, duration_s=15, **WINDOW)
    assert next_window(at(58, 20), NOW, **args) == at(59, 20)
    assert next_window(at(58, 30), NOW, **args) is None


def test_short_backlog_is_caught_up_in_order_but_a_long_outage_is_skipped():
    args = dict(interval_s=30, duration_s=30, **WINDOW)
    assert next_window(at(55), NOW, **args) == at(55, 30)  # 3 min de atraso: se recupera tramo a tramo
    assert next_window(at(0), NOW, **args) == at(59, 10)  # 1 h parado: salta al mas reciente


def test_window_start_has_whole_seconds():
    now = NOW + timedelta(microseconds=789_123)
    assert next_window(None, now, interval_s=30, duration_s=30, **WINDOW).microsecond == 0


# --- ingest_cameras: crea y encola los clips -------------------------------------------------


async def ingest_setup(session, *, with_detector: bool = True, **camera_fields):
    camera = await make_camera(session, None, "auto")
    camera.auto_ingest = True
    for field, value in camera_fields.items():
        setattr(camera, field, value)
    if with_detector:
        detector = Detector(name=f"p-{uuid.uuid4().hex[:6]}", kind="persons", base_url="http://p:8001")
        session.add(detector)
        await session.flush()
        session.add(CameraDetector(camera_id=camera.id, detector_id=detector.id))
    await session.flush()
    return camera


async def clips_of(session, camera) -> list[Clip]:
    stmt = select(Clip).where(Clip.camera_id == camera.id).order_by(Clip.started_at)
    return list((await session.execute(stmt)).scalars().all())


async def test_scheduler_creates_and_enqueues_contiguous_clips(session):
    camera, settings = await ingest_setup(session), Settings()

    assert await ingest_cameras(session, NOW, settings) >= 1
    (first,) = await clips_of(session, camera)
    assert first.source == ClipSource.SCHEDULE and first.status == ClipStatus.PENDING
    assert (first.started_at, first.duration_s) == (at(59, 10), 30)
    assert camera.last_ingest_start == at(59, 10)

    await ingest_cameras(session, NOW, settings)  # mismo instante: no toca otro tramo
    assert len(await clips_of(session, camera)) == 1

    await ingest_cameras(session, NOW + timedelta(seconds=30), settings)
    second = (await clips_of(session, camera))[1]
    assert second.started_at == first.started_at + timedelta(seconds=30)  # pegado al anterior


async def test_camera_without_active_services_downloads_nothing_but_keeps_pace(session):
    camera = await ingest_setup(session, with_detector=False)

    await ingest_cameras(session, NOW, Settings())

    assert await clips_of(session, camera) == []
    # avanza, para no acumular atraso si luego se activa un servicio
    assert camera.last_ingest_start == at(59, 10)


@pytest.mark.parametrize("fields", [{"auto_ingest": False}, {"enabled": False}])
async def test_disabled_cameras_are_not_ingested(session, fields):
    camera = await ingest_setup(session, **fields)

    await ingest_cameras(session, NOW, Settings())

    assert await clips_of(session, camera) == [] and camera.last_ingest_start is None


async def test_the_same_window_is_never_requested_twice(session):
    camera = await ingest_setup(session)
    await ingest_cameras(session, NOW, Settings())
    camera.last_ingest_start = None  # p. ej. un segundo programador que no vio el avance

    await ingest_cameras(session, NOW, Settings())

    assert len(await clips_of(session, camera)) == 1  # el indice unico lo impide, sin lanzar error


# --- Limpieza de videos ----------------------------------------------------------------------


async def test_old_videos_are_deleted_from_disk_but_their_records_are_kept(session, tmp_path):
    camera = await make_camera(session, None, "a")
    old_file, recent_file = tmp_path / "old.mp4", tmp_path / "recent.mp4"
    old_file.write_bytes(b"x")
    recent_file.write_bytes(b"x")
    old = await make_clip(session, camera, at(0))
    recent = await make_clip(session, camera, at(1))
    old.path, recent.path = str(old_file), str(recent_file)
    old.created_at = datetime.now(UTC) - timedelta(days=40)
    await session.flush()

    assert await cleanup_old_clips(session, Settings(clip_retention_days=30)) == 1

    assert not old_file.exists() and old.path is None
    assert recent_file.exists() and recent.path == str(recent_file)
    assert await session.get(Clip, old.id) is not None  # el registro y sus detecciones se conservan
