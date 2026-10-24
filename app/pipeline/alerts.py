"""Confirmacion de alertas: una alerta del servicio solo se da por valida si se repite.

Un servicio puede lanzar alertas sueltas con confianza baja (p. ej. un SOS en un frame borroso).
Una alerta queda confirmada cuando la misma persona (track_id) mantiene la misma alerta con
confianza >= `alert_min_confidence` durante `alert_min_frames` frames consecutivos del clip.
Las detecciones originales no se descartan; solo se marcan con `alert_confirmed`.
"""
from dataclasses import dataclass, field


@dataclass
class Confirmation:
    detector: str
    alert: str
    track_id: int | None
    frames: int
    first_offset_s: float
    offset_s: float
    max_confidence: float


@dataclass
class _Streak:
    last_frame: int = -1
    dets: list = field(default_factory=list)


class AlertTracker:
    """Estado de rachas de un clip. Los frames deben observarse en orden y con su indice real."""

    def __init__(self) -> None:
        self._streaks: dict[tuple, _Streak] = {}

    def observe(self, frame_idx: int, detector, detections: list) -> list[Confirmation]:
        """Registra las detecciones de un frame y devuelve las alertas que se confirman en el.

        `detections` son objetos con `alert`, `confidence`, `track_id`, `offset_s` y `alert_confirmed`
        (las filas Detection). Un frame sin llamada a observe (p. ej. el servicio fallo) corta la racha.
        """
        best: dict[tuple, object] = {}
        for d in detections:
            if d.alert and d.confidence >= detector.alert_min_confidence:
                key = (detector.id, d.track_id, d.alert)
                if key not in best or d.confidence > best[key].confidence:
                    best[key] = d

        confirmed = []
        for key, d in best.items():
            streak = self._streaks.get(key)
            if streak is None or streak.last_frame != frame_idx - 1:
                streak = self._streaks[key] = _Streak()
            streak.last_frame = frame_idx
            streak.dets.append(d)

            if len(streak.dets) == detector.alert_min_frames:
                for x in streak.dets:
                    x.alert_confirmed = True
                confirmed.append(Confirmation(
                    detector=detector.name, alert=d.alert, track_id=d.track_id, frames=len(streak.dets),
                    first_offset_s=streak.dets[0].offset_s, offset_s=d.offset_s,
                    max_confidence=max(x.confidence for x in streak.dets),
                ))
            elif len(streak.dets) > detector.alert_min_frames:
                d.alert_confirmed = True
        return confirmed
