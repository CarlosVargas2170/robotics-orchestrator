import time

import httpx

from app.detectors.adapters import ADAPTERS


async def probe(base_url: str, adapter: str, timeout_s: float = 5.0) -> dict:
    """Consulta el health check del servicio. Lanza ValueError si su adaptador no define uno."""
    health_path = ADAPTERS[adapter].health_path
    if health_path is None:
        raise ValueError(f"El adaptador {adapter!r} no define health check")

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(f"{base_url.rstrip('/')}{health_path}")
    except httpx.HTTPError as exc:
        return {"online": False, "status_code": None, "latency_ms": None, "detail": exc.__class__.__name__}

    latency_ms = round((time.perf_counter() - started) * 1000)
    try:
        detail = resp.json()
    except ValueError:
        detail = f"Respuesta no JSON (HTTP {resp.status_code}); el servicio o su tunel pueden estar apagados"
    return {"online": resp.is_success, "status_code": resp.status_code, "latency_ms": latency_ms, "detail": detail}
