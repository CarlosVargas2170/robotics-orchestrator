"""Validaciones contra robotics-backend: que un usuario o un robot existan antes de guardarlos por su id o codigo.

Las dos bases de datos son independientes y no hay claves foraneas entre ellas, asi que un error de tipeo crearia un
registro fantasma. Se pregunta al backend con el token del propio admin que hace la operacion (no hay credenciales
aparte). Si el backend no responde, se rechaza: es preferible reintentar que guardar algo sin validar.
"""
import uuid

from fastapi import HTTPException

from app.auth.backend import PRODUCT_ROLES, BackendUnavailable, CurrentUser, get_auth_client

_UNAVAILABLE = "No se pudo validar contra robotics-backend; intentalo de nuevo en un momento"


async def ensure_product_user(admin: CurrentUser, user_id: uuid.UUID) -> None:
    """422 si el usuario no existe, esta desactivado o su rol no usa este producto; 503 si no se pudo comprobar."""
    try:
        found = await get_auth_client().find_user(admin.token, user_id)
    except BackendUnavailable:
        raise HTTPException(503, _UNAVAILABLE) from None
    if found is None:
        raise HTTPException(422, f"El usuario {user_id} no existe en robotics-backend")
    if not found.get("active", True):
        raise HTTPException(422, f"El usuario {found.get('login', user_id)} esta desactivado")
    if found.get("role") not in PRODUCT_ROLES:
        raise HTTPException(
            422,
            f"El usuario {found.get('login', user_id)} tiene rol '{found.get('role')}': "
            "solo admin y cliente usan el reconocimiento de video",
        )


async def ensure_robot(admin: CurrentUser, code: str) -> None:
    """422 si el robot no existe en robotics-backend; 503 si no se pudo comprobar."""
    try:
        exists = await get_auth_client().robot_exists(admin.token, code)
    except BackendUnavailable:
        raise HTTPException(503, _UNAVAILABLE) from None
    if not exists:
        raise HTTPException(422, f"El robot '{code}' no existe en robotics-backend")
