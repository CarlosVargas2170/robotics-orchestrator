from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.backend import AuthError, BackendUnavailable, CurrentUser, RoleNotAllowed, get_auth_client

bearer_scheme = HTTPBearer(
    scheme_name="AccessToken",
    bearerFormat="JWT",
    description="Access token de robotics-backend, emitido por POST /api/auth/login.",
    auto_error=False,
)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> CurrentUser:
    if credentials is None:
        raise HTTPException(401, "Falta el token", headers={"WWW-Authenticate": "Bearer"})
    try:
        return await get_auth_client().authenticate(credentials.credentials)
    except AuthError:
        raise HTTPException(401, "Token invalido", headers={"WWW-Authenticate": "Bearer"}) from None
    except RoleNotAllowed:
        raise HTTPException(403, "Tu rol no tiene acceso a este producto") from None
    except BackendUnavailable:
        raise HTTPException(503, "Servicio de autenticacion no disponible") from None


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(403, "Se requiere rol admin")
    return user
