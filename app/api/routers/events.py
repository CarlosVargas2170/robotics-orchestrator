import asyncio
import json
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from app.auth.access import CameraAccess
from app.auth.backend import AuthError, BackendUnavailable, get_auth_client
from app.core.config import get_settings
from app.db.models import Camera
from app.db.session import SessionLocal
from app.pipeline.runner import EVENTS_CHANNEL

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
    except BackendUnavailable:
        await ws.close(code=1013)
        return
    except (AuthError, TimeoutError, WebSocketDisconnect, KeyError, TypeError, ValueError):
        await ws.close(code=4401)
        return

    access = CameraAccess(user, _owner_of)
    redis = Redis.from_url(get_settings().redis_url)
    pubsub = redis.pubsub()
    await pubsub.subscribe(EVENTS_CHANNEL)
    last_check = time.monotonic()
    try:
        while True:
            if time.monotonic() - last_check > REVALIDATE_EVERY_S:
                await auth.authenticate(token)
                access.forget()
                last_check = time.monotonic()
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is None:
                continue
            event = json.loads(msg["data"])
            if await access.allows(event["camera_id"]):
                await ws.send_text(msg["data"].decode())
    except (AuthError, BackendUnavailable):
        await ws.close(code=4401)
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe(EVENTS_CHANNEL)
        await redis.aclose()
