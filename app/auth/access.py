"""Visibilidad: un usuario solo ve los robots (camaras) que tiene asignados; el admin ve todos."""
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import CurrentUser
from app.db.models import Camera


def visible_cameras(stmt: Select, user: CurrentUser) -> Select:
    """Restringe una consulta que ya incluye `Camera` a las camaras del usuario."""
    return stmt if user.is_admin else stmt.where(Camera.owner_user_id == user.id)


async def get_visible_camera(session: AsyncSession, user: CurrentUser, camera_id: uuid.UUID) -> Camera | None:
    """La camara, o None si no existe o no es del usuario (el llamador responde 404 en ambos casos)."""
    camera = await session.get(Camera, camera_id)
    if camera is None or not (user.is_admin or camera.owner_user_id == user.id):
        return None
    return camera


class CameraAccess:
    """Decide si un usuario puede recibir eventos de una camara (para el WebSocket)."""

    def __init__(self, user: CurrentUser, owner_of: Callable[[uuid.UUID], Awaitable[uuid.UUID | None]]):
        self._user = user
        self._owner_of = owner_of
        self._cache: dict[str, bool] = {}

    async def allows(self, camera_id: str) -> bool:
        if self._user.is_admin:
            return True
        if camera_id not in self._cache:
            owner = await self._owner_of(uuid.UUID(camera_id))
            self._cache[camera_id] = owner == self._user.id
        return self._cache[camera_id]

    def forget(self) -> None:
        """Descarta lo aprendido para ver reasignaciones de camaras."""
        self._cache.clear()
