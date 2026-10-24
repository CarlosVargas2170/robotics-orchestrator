import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models import ClipSource, ClipStatus
from app.detectors.adapters import ADAPTERS
from app.detectors.validation import validate_service_url


class CameraIn(BaseModel):
    name: str
    stream_name: str
    owner_user_id: uuid.UUID | None = None
    robot_code: str | None = None
    merchant_id: int | None = None
    location: str | None = None
    enabled: bool = True
    # Ingesta automatica: un tramo de `ingest_duration_s` cada `ingest_interval_s` (igual = cobertura continua)
    auto_ingest: bool = False
    ingest_interval_s: int = Field(default=30, ge=5, le=3600)
    ingest_duration_s: int = Field(default=30, ge=5, le=300)

    @model_validator(mode="after")
    def _interval_not_shorter_than_duration(self) -> "CameraIn":
        if self.ingest_interval_s < self.ingest_duration_s:
            raise ValueError("ingest_interval_s no puede ser menor que ingest_duration_s (los tramos se solaparian)")
        return self


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


# --- Gestion de servicios de IA -------------------------------------------------------------


def _validate_adapter(v: str) -> str:
    if v not in ADAPTERS:
        raise ValueError(f"Adaptador desconocido; disponibles: {', '.join(ADAPTERS)}")
    return v


class _DetectorFields(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str = Field(min_length=1, max_length=20)  # persons | faces | animals | objects ...
    base_url: str = Field(max_length=200)
    adapter: str = "generic"
    enabled: bool = True
    min_confidence: float = Field(default=0.0, ge=0, le=1)
    timeout_s: float = Field(default=30.0, gt=0, le=300)
    alert_min_confidence: float = Field(default=0.7, ge=0, le=1)
    alert_min_frames: int = Field(default=3, ge=1, le=30)
    max_concurrency: int = Field(default=2, ge=1, le=20)

    @field_validator("base_url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        return validate_service_url(v)

    @field_validator("adapter")
    @classmethod
    def _check_adapter(cls, v: str) -> str:
        return _validate_adapter(v)


class DetectorIn(_DetectorFields):
    pass


class DetectorPatch(BaseModel):
    """Todos los campos son opcionales; solo se cambian los que se envian."""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    kind: str | None = Field(default=None, min_length=1, max_length=20)
    base_url: str | None = Field(default=None, max_length=200)
    adapter: str | None = None
    enabled: bool | None = None
    min_confidence: float | None = Field(default=None, ge=0, le=1)
    timeout_s: float | None = Field(default=None, gt=0, le=300)
    alert_min_confidence: float | None = Field(default=None, ge=0, le=1)
    alert_min_frames: int | None = Field(default=None, ge=1, le=30)
    max_concurrency: int | None = Field(default=None, ge=1, le=20)

    @field_validator("base_url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        return None if v is None else validate_service_url(v)

    @field_validator("adapter")
    @classmethod
    def _check_adapter(cls, v: str | None) -> str | None:
        return None if v is None else _validate_adapter(v)

    @model_validator(mode="after")
    def _no_explicit_nulls(self) -> "DetectorPatch":
        nulls = [f for f in self.model_fields_set if getattr(self, f) is None]
        if nulls:
            raise ValueError(f"Estos campos no admiten null: {', '.join(sorted(nulls))}")
        return self


class DetectorOut(_DetectorFields):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class DetectorSummary(BaseModel):
    """Lo que ve un usuario sin rol admin: sin la URL interna del servicio."""

    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    kind: str


class AccessIn(BaseModel):
    enabled: bool = True


class AccessOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user_id: uuid.UUID
    enabled: bool


class CameraPatch(BaseModel):
    """Todos los campos son opcionales; `owner_user_id: null` deja la camara sin dueño."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    owner_user_id: uuid.UUID | None = None
    robot_code: str | None = None
    merchant_id: int | None = None
    location: str | None = None
    enabled: bool | None = None
    auto_ingest: bool | None = None
    ingest_interval_s: int | None = Field(default=None, ge=5, le=3600)
    ingest_duration_s: int | None = Field(default=None, ge=5, le=300)


class CameraDetectorIn(BaseModel):
    enabled: bool = True


class CameraDetectorOut(BaseModel):
    detector_id: uuid.UUID
    name: str
    kind: str
    enabled: bool
