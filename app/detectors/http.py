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
        adapter: str = "generic",
        client: httpx.AsyncClient | None = None,
    ):
        if adapter not in ADAPTERS:
            raise ValueError(f"Adaptador desconocido: {adapter!r} (disponibles: {', '.join(ADAPTERS)})")
        self.id = id or uuid.uuid4()
        self.name = name
        self.kind = kind
        self.min_confidence = min_confidence
        self._adapter = ADAPTERS[adapter]
        self._url = f"{base_url.rstrip('/')}{self._adapter.path}"
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def detect(self, frame: bytes, ctx: FrameContext) -> list[DetectionResult]:
        resp = await self._client.post(self._url, **self._adapter.build_request(frame, ctx))
        resp.raise_for_status()
        return self._adapter.parse(resp.json())

    async def aclose(self) -> None:
        await self._client.aclose()
