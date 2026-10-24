import uuid
from datetime import UTC, datetime

from app.db.models import Camera, Clip, ClipStatus

ADMIN_ID, USER_A, USER_B = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
BACKEND_USERS = {
    "tok-admin": {"id": str(ADMIN_ID), "login": "admin", "role": "admin", "active": True},
    "tok-a": {"id": str(USER_A), "login": "ana", "role": "operator", "active": True},
    "tok-b": {"id": str(USER_B), "login": "beto", "role": "operator", "active": True},
}


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
