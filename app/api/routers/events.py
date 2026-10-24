from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from app.core.config import get_settings
from app.pipeline.runner import EVENTS_CHANNEL

router = APIRouter(tags=["events"])


@router.websocket("/ws/events")
async def events(ws: WebSocket):
    """Stream en vivo de detecciones para Robot Control Interface."""
    await ws.accept()
    redis = Redis.from_url(get_settings().redis_url)
    pubsub = redis.pubsub()
    await pubsub.subscribe(EVENTS_CHANNEL)
    try:
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                await ws.send_text(msg["data"].decode())
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe(EVENTS_CHANNEL)
        await redis.aclose()
