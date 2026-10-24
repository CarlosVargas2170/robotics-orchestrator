"""Cola de trabajo sobre la tabla clips: orden por camara, recuperacion y reparto sin duplicados.

Usa el Postgres de DATABASE_URL; los tests con `session` se revierten, el de concurrencia usa datos propios
que borra al terminar.
"""
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.models import Camera, Clip, ClipStatus
from app.events import asyncpg_dsn
from app.queue import WAKE_CHANNEL, claim_next, notify_new_work, recover_stale
from tests.helpers import make_camera, make_clip

STALE = 600.0
# Muy antiguos para ir siempre delante de cualquier otro clip que hubiera en la BD
T0 = datetime(2000, 1, 1, 0, 0, 0, tzinfo=UTC)


def minute(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


async def test_oldest_pending_clip_is_claimed_and_marked_in_progress(session):
    camera = await make_camera(session, None, "a")
    first, second = await make_clip(session, camera, minute(0)), await make_clip(session, camera, minute(1))

    assert await claim_next(session, stale_s=STALE) == first.id
    await session.refresh(first)
    assert (first.status, first.attempts) == (ClipStatus.DOWNLOADING, 1)

    # El segundo espera: la camara tiene el anterior en proceso
    assert await claim_next(session, stale_s=STALE) != second.id

    first.status = ClipStatus.DONE
    await session.flush()
    assert await claim_next(session, stale_s=STALE) == second.id


async def test_a_camera_is_processed_in_order_but_other_cameras_are_not_blocked(session):
    busy, free = await make_camera(session, None, "busy"), await make_camera(session, None, "free")
    await make_clip(session, busy, minute(0), ClipStatus.PROCESSING)
    blocked = await make_clip(session, busy, minute(1))
    other = await make_clip(session, free, minute(2))

    assert await claim_next(session, stale_s=STALE) == other.id
    await session.refresh(blocked)
    assert blocked.status == ClipStatus.PENDING


@pytest.mark.parametrize("finished", [ClipStatus.DONE, ClipStatus.FAILED, ClipStatus.NO_DATA])
async def test_finished_clips_do_not_block_the_next_one(session, finished):
    camera = await make_camera(session, None, "a")
    await make_clip(session, camera, minute(0), finished)
    later = await make_clip(session, camera, minute(1))

    assert await claim_next(session, stale_s=STALE) == later.id


async def test_a_clip_stuck_for_too_long_does_not_block_the_camera(session):
    camera = await make_camera(session, None, "a")
    await make_clip(session, camera, minute(0), ClipStatus.PROCESSING, updated=datetime.now(UTC) - timedelta(hours=1))
    later = await make_clip(session, camera, minute(1))

    assert await claim_next(session, stale_s=STALE) == later.id


async def test_clips_left_halfway_are_requeued_until_attempts_run_out(session):
    camera = await make_camera(session, None, "a")
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    retry = await make_clip(session, camera, minute(0), ClipStatus.PROCESSING, updated=long_ago, attempts=1)
    exhausted = await make_clip(session, camera, minute(1), ClipStatus.DOWNLOADING, updated=long_ago, attempts=3)
    running = await make_clip(session, camera, minute(2), ClipStatus.PROCESSING, attempts=1)  # reciente: sigue vivo
    waiting = await make_clip(session, camera, minute(3), ClipStatus.PENDING, updated=long_ago)

    requeued, failed = await recover_stale(session, stale_s=STALE, max_attempts=3)

    assert (requeued, failed) == (1, 1)
    for clip in (retry, exhausted, running, waiting):
        await session.refresh(clip)
    assert retry.status == ClipStatus.PENDING
    assert exhausted.status == ClipStatus.FAILED and "reintentos" in exhausted.error
    assert running.status == ClipStatus.PROCESSING
    assert waiting.status == ClipStatus.PENDING


async def test_concurrent_workers_never_claim_the_same_clip():
    """Seis workers a la vez, seis clips pendientes de camaras distintas: cada uno toma uno distinto."""
    engine = create_async_engine(get_settings().database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    tag = uuid.uuid4().hex[:8]
    try:
        try:
            async with maker() as s:
                cameras = [Camera(name=f"q-{tag}-{i}", stream_name=f"q-{tag}-{i}") for i in range(6)]
                s.add_all(cameras)
                await s.flush()
                s.add_all(Clip(camera_id=c.id, started_at=minute(i), duration_s=30) for i, c in enumerate(cameras))
                await s.commit()
        except OSError:
            pytest.skip("PostgreSQL no disponible")

        async def worker():
            async with maker() as s:
                return await claim_next(s, stale_s=STALE)

        claimed = await asyncio.gather(*(worker() for _ in range(6)))

        assert None not in claimed and len(set(claimed)) == 6
        async with maker() as s:
            attempts = (await s.execute(select(Clip.attempts).where(Clip.id.in_(claimed)))).scalars().all()
        assert attempts == [1] * 6
    finally:
        async with maker() as s:
            ids = (await s.execute(select(Camera.id).where(Camera.name.like(f"q-{tag}-%")))).scalars().all()
            await s.execute(delete(Clip).where(Clip.camera_id.in_(ids)))
            await s.execute(delete(Camera).where(Camera.id.in_(ids)))
            await s.commit()
        await engine.dispose()


async def test_new_work_wakes_up_listening_workers():
    dsn = asyncpg_dsn(get_settings().database_url)
    try:
        listener = await asyncpg.connect(dsn)
    except OSError:
        pytest.skip("PostgreSQL no disponible")
    woke = asyncio.Event()
    await listener.add_listener(WAKE_CHANNEL, lambda *_: woke.set())
    engine = create_async_engine(get_settings().database_url)
    try:
        async with async_sessionmaker(engine)() as s:
            await notify_new_work(s)
        await asyncio.wait_for(woke.wait(), 5)
    finally:
        await listener.close()
        await engine.dispose()
