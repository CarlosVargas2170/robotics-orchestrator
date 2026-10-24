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
    false,
    func,
    text,
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
    NO_DATA = "no_data"  # Flussonic no tiene archivo de ese tramo (p. ej. el robot estaba apagado)


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
    # Ingesta automatica: cada `ingest_interval_s` se pide un tramo de `ingest_duration_s` segundos
    auto_ingest: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    ingest_interval_s: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    ingest_duration_s: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    last_ingest_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    clips: Mapped[list["Clip"]] = relationship(back_populates="camera")
    # Al borrar el robot se borran sus servicios activados (la BD ya lo hace con ON DELETE CASCADE)
    detector_links: Mapped[list["CameraDetector"]] = relationship(
        back_populates="camera", cascade="all, delete-orphan", passive_deletes=True
    )


class Clip(TimestampMixin, Base):
    __tablename__ = "clips"
    __table_args__ = (
        Index("ix_clips_camera_started", "camera_id", "started_at"),
        # Respaldo contra pedir dos veces el mismo tramo automatico (p. ej. dos programadores a la vez)
        Index(
            "uq_clips_scheduled_window", "camera_id", "started_at", unique=True,
            postgresql_where=text("source = 'SCHEDULE'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cameras.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[int] = mapped_column(Integer)
    path: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[ClipStatus] = mapped_column(Enum(ClipStatus), default=ClipStatus.PENDING, index=True)
    source: Mapped[ClipSource] = mapped_column(Enum(ClipSource), default=ClipSource.MANUAL)
    error: Mapped[str | None] = mapped_column(String(1000))
    # Veces que un worker lo ha tomado; si un worker cae a mitad, se reintenta hasta `max_attempts`
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
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
    # Una alerta se confirma si supera esta confianza en `alert_min_frames` frames consecutivos
    alert_min_confidence: Mapped[float] = mapped_column(Float, default=0.7, server_default="0.7")
    alert_min_frames: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    # Peticiones simultaneas al servicio por proceso del worker
    max_concurrency: Mapped[int] = mapped_column(Integer, default=2, server_default="2")


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
    # True cuando la alerta cumplio el criterio de confianza y repeticion del detector
    alert_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), index=True)
    raw: Mapped[dict | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    clip: Mapped[Clip] = relationship(back_populates="detections")
    detector: Mapped[Detector] = relationship()


class Alert(Base):
    """Una alerta confirmada (ver pipeline/alerts.py): una fila por alerta, no por frame.

    Las detecciones y los eventos en vivo son efimeros para quien no esta mirando; la alerta queda aqui hasta que
    alguien la atiende, de modo que el operador que abre la pantalla despues ve lo pendiente.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_camera_started", "camera_id", "started_at"),
        Index("ix_alerts_pending", "started_at", postgresql_where=text("acknowledged_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    clip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"), index=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cameras.id"))
    detector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("detectors.id"))
    alert: Mapped[str] = mapped_column(String(60), index=True)  # p. ej. person_requesting_help_sos
    track_id: Mapped[int | None] = mapped_column(Integer)
    # Instante real en que empezo la alerta (inicio del clip + first_offset_s)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_offset_s: Mapped[float] = mapped_column(Float)
    offset_s: Mapped[float] = mapped_column(Float)  # segundo del clip en que se confirmo
    frames: Mapped[int] = mapped_column(Integer)
    max_confidence: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Atencion: nulo mientras nadie la haya atendido
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # users.id de robotics-backend
    note: Mapped[str | None] = mapped_column(String(500))

    camera: Mapped[Camera] = relationship()


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
