from typing import Protocol

from pydantic import BaseModel, Field


class DetectionResult(BaseModel):
    """Contrato comun de salida de todos los servicios de deteccion."""

    label: str
    confidence: float = Field(ge=0, le=1)
    # [x, y, w, h] normalizado 0..1
    bbox: list[float] | None = None
    raw: dict | None = None


class Detector(Protocol):
    name: str

    async def detect(self, frame: bytes) -> list[DetectionResult]: ...
