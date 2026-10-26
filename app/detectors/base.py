import uuid
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field


class DetectionResult(BaseModel):
    """Salida normalizada de cualquier servicio de deteccion."""

    label: str
    confidence: float = Field(ge=0, le=1)
    # [x1, y1, x2, y2] en pixeles del frame
    bbox: list[float] | None = None
    track_id: int | None = None
    alert: str | None = None
    raw: dict | None = None


@dataclass(frozen=True)
class FrameContext:
    """Identifica el origen del frame; los servicios con tracking lo usan para no mezclar camaras."""

    robot_id: str
    timestamp: float  # epoch en segundos del instante real del frame
    camera_id: str | None = None  # cameras.id: distingue varias camaras de un mismo robot
    client_id: str | None = None  # cameras.owner_user_id (users.id de robotics-backend); nulo si no tiene dueño


class Detector(Protocol):
    id: uuid.UUID
    name: str
    kind: str
    min_confidence: float
    alert_min_confidence: float
    alert_min_frames: int

    async def detect(self, frame: bytes, ctx: FrameContext) -> list[DetectionResult]: ...
