import asyncio
import json
import logging
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Clip, ClipStatus, Detection
from app.detectors.base import Detector
from app.ingest.flussonic import FlussonicClient
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
    """Descarga el clip, extrae frames, corre los detectores en paralelo y guarda resultados."""
    try:
        clip.status = ClipStatus.DOWNLOADING
        await session.commit()
        dest = Path(settings.clips_dir) / f"clip_{clip.id}.mp4"
        await flussonic.download_clip(clip.camera.stream_name, clip.started_at, clip.duration_s, dest)
        clip.path = str(dest)

        clip.status = ClipStatus.PROCESSING
        await session.commit()
        frames = await extract_frames(dest, settings.frame_sample_fps)

        for offset_s, frame in frames:
            results = await asyncio.gather(*(d.detect(frame) for d in detectors), return_exceptions=True)
            for detector, res in zip(detectors, results, strict=True):
                if isinstance(res, Exception):
                    log.warning("detector %s fallo en clip %s: %s", detector.name, clip.id, res)
                    continue
                for r in res:
                    det = Detection(
                        clip_id=clip.id, detector=detector.name, label=r.label,
                        confidence=r.confidence, offset_s=offset_s, bbox=r.bbox, raw=r.raw,
                    )
                    session.add(det)
                    await redis.publish(EVENTS_CHANNEL, json.dumps({
                        "clip_id": clip.id, "camera_id": clip.camera_id, "detector": detector.name,
                        "label": r.label, "confidence": r.confidence, "offset_s": offset_s, "bbox": r.bbox,
                    }))
        clip.status = ClipStatus.DONE
    except Exception as exc:
        log.exception("clip %s fallo", clip.id)
        clip.status = ClipStatus.FAILED
        clip.error = str(exc)[:1000]
    await session.commit()
