"""Autenticacion delegada en robotics-backend.

El orquestador no guarda usuarios ni contraseñas: reenvia el access token del usuario a
GET /api/auth/me, que responde quien es (id, login, rol) o 401 si el token no vale o el usuario
esta desactivado. El resultado se cachea unos segundos para no consultar en cada peticion.
"""
import hashlib
import time
import uuid
from dataclasses import dataclass, field

import httpx

from app.core.config import get_settings

_MAX_CACHED = 1000

# Roles de robotics-backend que usan el reconocimiento de video. `operator` es del control de robots.
PRODUCT_ROLES = frozenset({"admin", "cliente"})


class AuthError(Exception):
    """Token ausente, invalido, vencido o de un usuario desactivado."""


class RoleNotAllowed(Exception):
    """Usuario valido, pero su rol no usa este producto (p. ej. un `operator`, que maneja el control de robots)."""


class BackendUnavailable(Exception):
    """robotics-backend no responde o no esta configurado."""


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    login: str
    role: str
    # Token de ESTA peticion, para consultar a robotics-backend en nombre del usuario. Lo pone get_current_user;
    # nunca se guarda en la cache de autenticacion ni sale en logs.
    token: str = field(default="", repr=False, compare=False)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class BackendAuthClient:
    def __init__(self, base_url: str, ttl_s: float, client: httpx.AsyncClient | None = None):
        self._base = base_url.rstrip("/")
        self._url = f"{self._base}/api/auth/me"
        self._ttl = ttl_s
        self._client = client or httpx.AsyncClient(timeout=5.0)
        self._cache: dict[str, tuple[float, CurrentUser]] = {}

    async def authenticate(self, token: str) -> CurrentUser:
        user = await self._resolve(token)
        if user.role not in PRODUCT_ROLES:  # tambien con el usuario en cache: el rol se comprueba siempre
            raise RoleNotAllowed(user.role)
        return user

    async def _resolve(self, token: str) -> CurrentUser:
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

    async def _backend_get(self, token: str, path: str, **params):
        """GET a robotics-backend con el token del propio usuario. Cualquier fallo es BackendUnavailable."""
        try:
            resp = await self._client.get(
                f"{self._base}{path}", params=params or None, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"robotics-backend no responde: {exc.__class__.__name__}") from exc
        if resp.status_code != 200:
            raise BackendUnavailable(f"robotics-backend respondio {resp.status_code} en {path}")
        return resp.json()

    async def find_user(self, token: str, user_id: uuid.UUID) -> dict | None:
        """El usuario de robotics-backend (id, login, role, active), o None si no existe. Requiere token de admin."""
        users = await self._backend_get(token, "/api/auth/users")
        return next((u for u in users if u.get("id") == str(user_id)), None)

    async def robot_exists(self, token: str, code: str) -> bool:
        """Si el robot existe en robotics-backend, aunque este inactivo. Requiere token de panel (admin)."""
        robots = await self._backend_get(token, "/api/robots", include_inactive="true")
        return any(r.get("code") == code for r in robots)

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
