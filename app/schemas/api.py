import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.models import ClipSource, ClipStatus


class CameraIn(BaseModel):
    name: str
    stream_name: str
    owner_user_id: uuid.UUID | None = None
    robot_code: str | None = None
    merchant_id: int | None = None
    location: str | None = None
    enabled: bool = True


class CameraOut(CameraIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class ClipIn(BaseModel):
    camera_id: uuid.UUID
    started_at: datetime
    duration_s: int = 10


class ClipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    camera_id: uuid.UUID
    started_at: datetime
    duration_s: int
    status: ClipStatus
    source: ClipSource
    requested_by: uuid.UUID | None
    error: str | None


class DetectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    clip_id: uuid.UUID
    detector_id: uuid.UUID
    label: str
    confidence: float
    offset_s: float
    bbox: list[float] | None
    track_id: int | None
    alert: str | None
    alert_confirmed: bool
