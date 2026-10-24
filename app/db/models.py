"""Modelo de datos del orquestador.

Los usuarios, robots y comercios viven en robotics-backend (otra base de datos), asi que aqui
solo se guardan referencias blandas (`owner_user_id`, `robot_code`, `merchant_id`) sin FK.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

JsonType = JSON().with_variant(JSONB(), "postgresql")


class ClipStatus(enum.StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class ClipSource(enum.StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    EVENT = "event"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Camera(TimestampMixin, Base):
    __tablename__ = "cameras"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    # Nombre del stream en Flussonic
    stream_name: Mapped[str] = mapped_column(String(120), unique=True)
    # Referencias blandas a robotics-backend (sin FK: otra base de datos)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)  # users.id
    robot_code: Mapped[str | None] = mapped_column(String(30), index=True)  # robots.code
    merchant_id: Mapped[int | None] = mapped_column(Integer)  # merchants.merchant_id
    location: Mapped[str | None] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())

    clips: Mapped[list["Clip"]] = relationship(back_populates="camera")
    detector_links: Mapped[list["CameraDetector"]] = relationship(back_populates="camera")


class Clip(TimestampMixin, Base):
    __tablename__ = "clips"
    __table_args__ = (Index("ix_clips_camera_started", "camera_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cameras.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[int] = mapped_column(Integer)
    path: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[ClipStatus] = mapped_column(Enum(ClipStatus), default=ClipStatus.PENDING, index=True)
    source: Mapped[ClipSource] = mapped_column(Enum(ClipSource), default=ClipSource.MANUAL)
    error: Mapped[str | None] = mapped_column(String(1000))
    # users.id de robotics-backend; nulo si lo disparo un horario o un evento
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    camera: Mapped[Camera] = relationship(back_populates="clips")
    detections: Mapped[list["Detection"]] = relationship(back_populates="clip")


class Detector(TimestampMixin, Base):
    """Catalogo de servicios de IA a los que el orquestador sabe enviar frames."""

    __tablename__ = "detectors"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)  # objects | faces | animals | persons
    base_url: Mapped[str] = mapped_column(String(200))
    # Como hablar con el servicio y normalizar su respuesta (ver app/detectors/adapters.py)
    adapter: Mapped[str] = mapped_column(String(40), default="generic", server_default="generic")
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())
    min_confidence: Mapped[float] = mapped_column(Float, server_default="0")
    timeout_s: Mapped[float] = mapped_column(Float, server_default="30")


class DetectorAccess(TimestampMixin, Base):
    """Que servicios puede usar cada usuario (permiso, lo concede un admin)."""

    __tablename__ = "detector_access"

    detector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("detectors.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)  # users.id de robotics-backend
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())


class CameraDetector(TimestampMixin, Base):
    """Que servicios activa cada camara (subconjunto de lo que permite su dueño)."""

    __tablename__ = "camera_detectors"

    camera_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), primary_key=True)
    detector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("detectors.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())

    camera: Mapped[Camera] = relationship(back_populates="detector_links")
    detector: Mapped[Detector] = relationship()


class Detection(Base):
    __tablename__ = "detections"
    __table_args__ = (Index("ix_detections_label_created", "label", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    clip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clips.id"), index=True)
    detector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("detectors.id"), index=True)
    label: Mapped[str] = mapped_column(String(120))
    confidence: Mapped[float] = mapped_column(Float)
    # Segundos desde el inicio del clip
    offset_s: Mapped[float] = mapped_column(Float)
    # [x1, y1, x2, y2] en pixeles del frame analizado
    bbox: Mapped[list[float] | None] = mapped_column(JsonType)
    track_id: Mapped[int | None] = mapped_column(Integer)
    # Alerta del servicio (p. ej. person_requesting_help_sos); nulo si no hay
    alert: Mapped[str | None] = mapped_column(String(60), index=True)
    raw: Mapped[dict | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    clip: Mapped[Clip] = relationship(back_populates="detections")
    detector: Mapped[Detector] = relationship()


class AuditLog(Base):
    """Mismo formato que audit_log de robotics-backend, para poder unificarlas despues."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(60))
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(45))
    details: Mapped[dict | list | None] = mapped_column(JsonType)
