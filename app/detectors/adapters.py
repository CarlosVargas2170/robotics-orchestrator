"""Un adaptador por tipo de servicio: arma la peticion y normaliza la respuesta a DetectionResult.

Agregar un servicio con otro contrato = registrar aqui un adaptador nuevo y usar su nombre
en `detectors.adapter`; el resto del orquestador no cambia.
"""
from collections.abc import Callable
from dataclasses import dataclass

from app.detectors.base import DetectionResult, FrameContext


@dataclass(frozen=True)
class Adapter:
    path: str
    build_request: Callable[[bytes, FrameContext], dict]  # kwargs para httpx.post
    parse: Callable[[dict], list[DetectionResult]]
    health_path: str | None = None  # GET que indica si el servicio esta listo; None si no tiene
    description: str = ""  # para el catalogo que ve el admin al registrar un servicio


def _generic_request(frame: bytes, ctx: FrameContext) -> dict:
    return {"files": {"image": ("frame.jpg", frame, "image/jpeg")}}


def _generic_parse(body: dict) -> list[DetectionResult]:
    # {"detections": [{"label": str, "confidence": float, "bbox": [x1, y1, x2, y2]?}]}
    return [DetectionResult(**d, raw=d) for d in body.get("detections", [])]


def _person_request(frame: bytes, ctx: FrameContext) -> dict:
    return {
        "files": {"file": ("frame.jpg", frame, "image/jpeg")},
        "data": {"robot_id": ctx.robot_id, "timestamp": str(ctx.timestamp)},
    }


def _person_parse(body: dict) -> list[DetectionResult]:
    """Servicio 1 (personas, swagger v2.1.0): POST /api/v1/analyze/frame."""
    if not body.get("detected"):
        return []
    evidence = body.get("last_incident_evidence")
    results = []
    for d in body.get("detections", []):
        box = d.get("bbox")
        raw = dict(d)
        if evidence:
            raw["incident_evidence"] = evidence  # clip Black Box grabado por el servicio
        results.append(DetectionResult(
            label="person",
            confidence=d["confidence"],
            bbox=[box["x1"], box["y1"], box["x2"], box["y2"]] if box else None,
            track_id=d.get("track_id"),
            alert=d.get("alert"),
            raw=raw,
        ))
    return results


ADAPTERS: dict[str, Adapter] = {
    "generic": Adapter(
        "/detect", _generic_request, _generic_parse,
        description="Formato propio: POST /detect con la imagen en el campo `image`; responde "
                    "{\"detections\": [{\"label\", \"confidence\", \"bbox\"}]}. Sin chequeo de salud.",
    ),
    "person_detection_v1": Adapter(
        "/api/v1/analyze/frame", _person_request, _person_parse, health_path="/api/v1/health",
        description="Servicio 1 (v2.1.0): deteccion, rastreo y seguridad de personas (caidas, SOS, inmovilidad). "
                    "POST /api/v1/analyze/frame y GET /api/v1/health.",
    ),
}
