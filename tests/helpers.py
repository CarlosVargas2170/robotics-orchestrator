import uuid
from datetime import UTC, datetime

from app.db.models import Camera, Clip, ClipStatus

ADMIN_ID, USER_A, USER_B, OPERATOR_ID, INACTIVE_ID = (uuid.uuid4() for _ in range(5))
BACKEND_USERS = {
    "tok-admin": {"id": str(ADMIN_ID), "login": "admin", "role": "admin", "active": True},
    "tok-a": {"id": str(USER_A), "login": "ana", "role": "cliente", "active": True},
    "tok-b": {"id": str(USER_B), "login": "beto", "role": "cliente", "active": True},
    # Maneja el control de robots (otro producto): no debe entrar al reconocimiento de video
    "tok-op": {"id": str(OPERATOR_ID), "login": "olga", "role": "operator", "active": True},
}

# Lo que devuelve GET /api/auth/users de robotics-backend: los usuarios con token mas uno desactivado
DIRECTORY_USERS = [*BACKEND_USERS.values(),
                   {"id": str(INACTIVE_ID), "login": "ines", "role": "cliente", "active": False}]
# Lo que devuelve GET /api/robots?include_inactive=true
DIRECTORY_ROBOTS = [
    {"code": "SERBOT01", "name": "Serbot 1", "active": True},
    {"code": "SERBOT02", "name": "Serbot 2", "active": True},
    {"code": "VIEJO", "name": "Robot retirado", "active": False},
]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def make_camera(session, owner: uuid.UUID | None, name: str) -> Camera:
    camera = Camera(name=f"{name}-{uuid.uuid4().hex[:6]}", stream_name=f"{name}-{uuid.uuid4().hex[:6]}",
                    owner_user_id=owner)
    session.add(camera)
    await session.flush()
    return camera


async def make_clip(session, camera, started: datetime, status=ClipStatus.PENDING, updated: datetime | None = None,
                    attempts: int = 0) -> Clip:
    clip = Clip(camera_id=camera.id, started_at=started, duration_s=30, status=status, attempts=attempts,
                updated_at=updated or datetime.now(UTC))
    session.add(clip)
    await session.flush()
    return clip


class FakePublisher:
    """Sustituye al publicador de eventos de la API (NOTIFY de PostgreSQL)."""

    def __init__(self):
        self.published: list[str] = []

    async def publish(self, message: str) -> None:
        self.published.append(message)
