import asyncio
import json
import logging
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Clip, ClipStatus, Detection
from app.detectors.base import Detector, FrameContext
from app.ingest.flussonic import FlussonicClient
from app.pipeline.alerts import AlertTracker
from app.pipeline.frames import extract_frames

log = logging.getLogger(__name__)

EVENTS_CHANNEL = "detections"


async def process_clip(
    session: AsyncSession,
    clip: Clip,
    settings: Settings,
    flussonic: FlussonicClient,
    detectors: list[Detector],
    redis: Redis,
) -> None:
    """Descarga el clip, extrae frames, corre los detectores en paralelo y guarda resultados.

    `detectors` ya viene filtrado por camara y permisos del dueño (ver detectors_for_camera).
    """
    if not detectors:
        log.info("clip %s: la camara %s no tiene servicios activos", clip.id, clip.camera_id)
        clip.status = ClipStatus.DONE
        await session.commit()
        return
    try:
        clip.status = ClipStatus.DOWNLOADING
        await session.commit()
        dest = Path(settings.clips_dir) / f"clip_{clip.id}.mp4"
        await flussonic.download_clip(clip.camera.stream_name, clip.started_at, clip.duration_s, dest)
        clip.path = str(dest)

        clip.status = ClipStatus.PROCESSING
        await session.commit()
        frames = await extract_frames(dest, settings.frame_sample_fps)

        # Los servicios con tracking necesitan saber de que camara y en que instante real es cada frame
        robot_id = clip.camera.robot_code or clip.camera.stream_name
        tracker = AlertTracker()
        for idx, (offset_s, frame) in enumerate(frames):
            ctx = FrameContext(robot_id, clip.started_at.timestamp() + offset_s)
            results = await asyncio.gather(*(d.detect(frame, ctx) for d in detectors), return_exceptions=True)
            for detector, res in zip(detectors, results, strict=True):
                if isinstance(res, Exception):
                    log.warning("detector %s fallo en clip %s: %s", detector.name, clip.id, res)
                    continue
                rows = [
                    Detection(
                        clip_id=clip.id, detector_id=detector.id, label=r.label,
                        confidence=r.confidence, offset_s=offset_s, bbox=r.bbox,
                        track_id=r.track_id, alert=r.alert, raw=r.raw,
                    )
                    for r in res
                    if r.confidence >= detector.min_confidence
                ]
                session.add_all(rows)
                for d in rows:
                    await _publish(redis, "detection", clip, detector=detector.name, kind=detector.kind,
                                   label=d.label, confidence=d.confidence, offset_s=d.offset_s, bbox=d.bbox,
                                   track_id=d.track_id, alert=d.alert)
                for c in tracker.observe(idx, detector, rows):
                    await _publish(redis, "alert_confirmed", clip, detector=c.detector, alert=c.alert,
                                   track_id=c.track_id, frames=c.frames, first_offset_s=c.first_offset_s,
                                   offset_s=c.offset_s, confidence=c.max_confidence)
        clip.status = ClipStatus.DONE
    except Exception as exc:
        log.exception("clip %s fallo", clip.id)
        clip.status = ClipStatus.FAILED
        clip.error = str(exc)[:1000]
    await session.commit()


async def _publish(redis: Redis, event_type: str, clip: Clip, **fields) -> None:
    """Evento para Robot Control Interface: `detection` por cada deteccion y `alert_confirmed` una vez por alerta."""
    await redis.publish(EVENTS_CHANNEL, json.dumps({
        "type": event_type, "clip_id": str(clip.id), "camera_id": str(clip.camera_id), **fields,
    }))
