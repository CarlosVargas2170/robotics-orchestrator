"""Autenticacion delegada en robotics-backend.

El orquestador no guarda usuarios ni contraseñas: reenvia el access token del usuario a
GET /api/auth/me, que responde quien es (id, login, rol) o 401 si el token no vale o el usuario
esta desactivado. El resultado se cachea unos segundos para no consultar en cada peticion.
"""
import hashlib
import time
import uuid
from dataclasses import dataclass

import httpx

from app.core.config import get_settings

_MAX_CACHED = 1000


class AuthError(Exception):
    """Token ausente, invalido, vencido o de un usuario desactivado."""


class BackendUnavailable(Exception):
    """robotics-backend no responde o no esta configurado."""


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    login: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class BackendAuthClient:
    def __init__(self, base_url: str, ttl_s: float, client: httpx.AsyncClient | None = None):
        self._url = f"{base_url.rstrip('/')}/api/auth/me"
        self._ttl = ttl_s
        self._client = client or httpx.AsyncClient(timeout=5.0)
        self._cache: dict[str, tuple[float, CurrentUser]] = {}

    async def authenticate(self, token: str) -> CurrentUser:
        key = hashlib.sha256(token.encode()).hexdigest()  # el token en claro no se conserva
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]

        try:
            resp = await self._client.get(self._url, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"robotics-backend no responde: {exc.__class__.__name__}") from exc
        if resp.status_code in (401, 403):
            raise AuthError("Token invalido")
        if resp.status_code != 200:
            raise BackendUnavailable(f"robotics-backend respondio {resp.status_code}")

        data = resp.json()
        if not data.get("active", True):
            raise AuthError("Usuario desactivado")
        user = CurrentUser(id=uuid.UUID(data["id"]), login=data["login"], role=data["role"])

        if len(self._cache) >= _MAX_CACHED:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        self._cache[key] = (now + self._ttl, user)
        return user

    async def aclose(self) -> None:
        await self._client.aclose()


_client: BackendAuthClient | None = None


def get_auth_client() -> BackendAuthClient:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.robotics_backend_url:
            raise BackendUnavailable("ROBOTICS_BACKEND_URL no esta configurada")
        _client = BackendAuthClient(settings.robotics_backend_url, settings.auth_cache_seconds)
    return _client


async def close_auth_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
