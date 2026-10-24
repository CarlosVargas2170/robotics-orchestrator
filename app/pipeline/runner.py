import asyncio
import json
import logging
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Alert, Clip, ClipStatus, Detection
from app.detectors.base import Detector, FrameContext
from app.events import EventPublisher
from app.ingest.flussonic import FlussonicClient, NoArchive
from app.pipeline.alerts import AlertTracker
from app.pipeline.frames import extract_frames

log = logging.getLogger(__name__)


async def process_clip(
    session: AsyncSession,
    clip: Clip,
    settings: Settings,
    flussonic: FlussonicClient,
    detectors: list[Detector],
    events: EventPublisher,
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
        if clip.attempts > 1:
            # Reintento tras caerse un worker: se descarta lo que dejo guardado para no duplicarlo
            await session.execute(delete(Alert).where(Alert.clip_id == clip.id))
            await session.execute(delete(Detection).where(Detection.clip_id == clip.id))

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
        has_evidence = False
        for idx, (offset_s, frame) in enumerate(frames):
            ctx = FrameContext(robot_id, clip.started_at.timestamp() + offset_s)
            results = await asyncio.gather(*(d.detect(frame, ctx) for d in detectors), return_exceptions=True)
            pending_events: list[tuple[str, dict]] = []
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
                    pending_events.append(("detection", dict(
                        detector=detector.name, kind=detector.kind, label=d.label, confidence=d.confidence,
                        offset_s=d.offset_s, bbox=d.bbox, track_id=d.track_id, alert=d.alert)))
                for c in tracker.observe(idx, detector, rows):
                    has_evidence = True
                    alert = Alert(
                        id=uuid.uuid4(), clip_id=clip.id, camera_id=clip.camera_id, detector_id=c.detector_id,
                        alert=c.alert, track_id=c.track_id, frames=c.frames, max_confidence=c.max_confidence,
                        first_offset_s=c.first_offset_s, offset_s=c.offset_s,
                        started_at=clip.started_at + timedelta(seconds=c.first_offset_s),
                    )
                    session.add(alert)
                    pending_events.append(("alert_confirmed", dict(
                        alert_id=str(alert.id), detector=c.detector, alert=c.alert, track_id=c.track_id,
                        frames=c.frames, first_offset_s=c.first_offset_s, offset_s=c.offset_s,
                        confidence=c.max_confidence, started_at=alert.started_at.isoformat())))
            # Primero se guarda y despues se avisa: cuando el frontend recibe un evento y consulta, ya existe
            await session.commit()
            for event_type, fields in pending_events:
                await _publish(events, event_type, clip, **fields)
        clip.status = ClipStatus.DONE
        if has_evidence or settings.keep_clips:
            pass  # el video con una alerta confirmada se conserva como evidencia hasta la limpieza periodica
        else:
            dest.unlink(missing_ok=True)  # sin alertas no hay motivo para guardar ~4 MB por clip
            clip.path = None
    except NoArchive:
        log.info("clip %s: Flussonic no tiene archivo de ese tramo", clip.id)
        clip.status = ClipStatus.NO_DATA
        clip.error = "Sin archivo en Flussonic para ese tramo"
    except Exception as exc:
        log.exception("clip %s fallo", clip.id)
        clip.status = ClipStatus.FAILED
        clip.error = str(exc)[:1000]
    await session.commit()


async def _publish(events: EventPublisher, event_type: str, clip: Clip, **fields) -> None:
    """Evento para Robot Control Interface: `detection` por cada deteccion y `alert_confirmed` una vez por alerta."""
    await events.publish(json.dumps({
        "type": event_type, "clip_id": str(clip.id), "camera_id": str(clip.camera_id), **fields,
    }))
