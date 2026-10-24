import uuid

import httpx

from app.detectors.base import DetectionResult


class HttpDetector:
    """Adaptador para un servicio de IA por HTTP.

    Contrato esperado: POST {base_url}/detect (multipart, campo `image` con un JPEG)
    -> {"detections": [{"label": str, "confidence": float, "bbox": [x, y, w, h]?}]}
    """

    def __init__(
        self,
        name: str,
        base_url: str,
        timeout_s: float,
        *,
        id: uuid.UUID | None = None,
        kind: str = "",
        min_confidence: float = 0.0,
        client: httpx.AsyncClient | None = None,
    ):
        self.id = id or uuid.uuid4()
        self.name = name
        self.kind = kind
        self.min_confidence = min_confidence
        self._url = f"{base_url.rstrip('/')}/detect"
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def detect(self, frame: bytes) -> list[DetectionResult]:
        resp = await self._client.post(self._url, files={"image": ("frame.jpg", frame, "image/jpeg")})
        resp.raise_for_status()
        return [DetectionResult(**d, raw=d) for d in resp.json().get("detections", [])]

    async def aclose(self) -> None:
        await self._client.aclose()
