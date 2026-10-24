import asyncio
import json
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.auth.access import CameraAccess
from app.auth.backend import AuthError, BackendUnavailable, RoleNotAllowed, get_auth_client
from app.db.models import Camera
from app.db.session import SessionLocal

router = APIRouter(tags=["events"])

AUTH_TIMEOUT_S = 5
REVALIDATE_EVERY_S = 60


async def _owner_of(camera_id: uuid.UUID) -> uuid.UUID | None:
    async with SessionLocal() as session:
        camera = await session.get(Camera, camera_id)
    return camera.owner_user_id if camera else None


@router.websocket("/ws/events")
async def events(ws: WebSocket):
    """Stream en vivo de eventos de las camaras del usuario.

    El primer mensaje del cliente debe ser {"token": "<access token>"}. Cada minuto se vuelve a
    validar el token: al vencer (o si desactivan al usuario) el servidor cierra con 4401 y el
    cliente debe reconectar con un token nuevo.
    """
    await ws.accept()
    auth = None
    try:
        auth = get_auth_client()
        token = (await asyncio.wait_for(ws.receive_json(), AUTH_TIMEOUT_S))["token"]
        user = await auth.authenticate(token)
    except RoleNotAllowed:
        await ws.close(code=4403)
        return
    except BackendUnavailable:
        await ws.close(code=1013)
        return
    except (AuthError, TimeoutError, WebSocketDisconnect, KeyError, TypeError, ValueError):
        await ws.close(code=4401)
        return

    access = CameraAccess(user, _owner_of)
    bus = ws.app.state.events
    queue = bus.subscribe()
    last_check = time.monotonic()
    try:
        while True:
            if time.monotonic() - last_check > REVALIDATE_EVERY_S:
                await auth.authenticate(token)
                access.forget()
                last_check = time.monotonic()
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            if await access.allows(json.loads(payload)["camera_id"]):
                await ws.send_text(payload)
    except RoleNotAllowed:
        await ws.close(code=4403)
    except (AuthError, BackendUnavailable):
        await ws.close(code=4401)
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(queue)
