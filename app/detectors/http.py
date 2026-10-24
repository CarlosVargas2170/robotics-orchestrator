import asyncio
import uuid

import httpx

from app.detectors.adapters import ADAPTERS
from app.detectors.base import DetectionResult, FrameContext


class HttpDetector:
    """Cliente de un servicio de IA por HTTP; el contrato concreto lo define su adaptador."""

    def __init__(
        self,
        name: str,
        base_url: str,
        timeout_s: float,
        *,
        id: uuid.UUID | None = None,
        kind: str = "",
        min_confidence: float = 0.0,
        alert_min_confidence: float = 0.7,
        alert_min_frames: int = 3,
        max_concurrency: int = 2,
        adapter: str = "generic",
        client: httpx.AsyncClient | None = None,
    ):
        if adapter not in ADAPTERS:
            raise ValueError(f"Adaptador desconocido: {adapter!r} (disponibles: {', '.join(ADAPTERS)})")
        self.id = id or uuid.uuid4()
        self.name = name
        self.kind = kind
        self.min_confidence = min_confidence
        self.alert_min_confidence = alert_min_confidence
        self.alert_min_frames = alert_min_frames
        self._adapter = ADAPTERS[adapter]
        self._url = f"{base_url.rstrip('/')}{self._adapter.path}"
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        # Compartido por todos los clips que procesa este worker: evita saturar el servicio
        self._slots = asyncio.Semaphore(max(1, max_concurrency))

    async def detect(self, frame: bytes, ctx: FrameContext) -> list[DetectionResult]:
        async with self._slots:
            resp = await self._client.post(self._url, **self._adapter.build_request(frame, ctx))
        resp.raise_for_status()
        return self._adapter.parse(resp.json())

    async def aclose(self) -> None:
        await self._client.aclose()
