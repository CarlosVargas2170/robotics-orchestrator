from datetime import datetime
from pathlib import Path

import httpx

from app.core.config import Settings


class FlussonicClient:
    """Cliente minimo de Flussonic: listar streams y descargar clips del DVR."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        auth = None
        if settings.flussonic_user:
            auth = httpx.BasicAuth(settings.flussonic_user, settings.flussonic_password)
        self._base = settings.flussonic_url.rstrip("/")
        self._client = client or httpx.AsyncClient(auth=auth, timeout=60.0)

    def clip_url(self, stream_name: str, started_at: datetime, duration_s: int) -> str:
        """URL de exportacion del archivo: /<stream>/archive-<from>-<duration>.mp4"""
        return f"{self._base}/{stream_name}/archive-{int(started_at.timestamp())}-{duration_s}.mp4"

    async def list_streams(self) -> list[dict]:
        resp = await self._client.get(f"{self._base}/streamer/api/v3/streams")
        resp.raise_for_status()
        return resp.json().get("streams", [])

    async def download_clip(
        self, stream_name: str, started_at: datetime, duration_s: int, dest: Path
    ) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = self.clip_url(stream_name, started_at, duration_s)
        async with self._client.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
        return dest

    async def aclose(self) -> None:
        await self._client.aclose()
