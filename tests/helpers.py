import uuid

from app.db.models import Camera

ADMIN_ID, USER_A, USER_B = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
BACKEND_USERS = {
    "tok-admin": {"id": str(ADMIN_ID), "login": "admin", "role": "admin", "active": True},
    "tok-a": {"id": str(USER_A), "login": "ana", "role": "operator", "active": True},
    "tok-b": {"id": str(USER_B), "login": "beto", "role": "operator", "active": True},
}


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class FakeQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, args))


async def make_camera(session, owner: uuid.UUID | None, name: str) -> Camera:
    camera = Camera(name=f"{name}-{uuid.uuid4().hex[:6]}", stream_name=f"{name}-{uuid.uuid4().hex[:6]}",
                    owner_user_id=owner)
    session.add(camera)
    await session.flush()
    return camera
