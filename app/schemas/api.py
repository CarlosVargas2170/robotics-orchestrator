from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.models import ClipStatus


class CameraIn(BaseModel):
    name: str
    stream_name: str
    enabled: bool = True


class CameraOut(CameraIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class ClipIn(BaseModel):
    camera_id: int
    started_at: datetime
    duration_s: int = 10


class ClipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    camera_id: int
    started_at: datetime
    duration_s: int
    status: ClipStatus
    error: str | None


class DetectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    clip_id: int
    detector: str
    label: str
    confidence: float
    offset_s: float
    bbox: list[float] | None
