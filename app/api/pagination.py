"""Ayudas comunes de los listados: fechas en UTC, limites de pagina y el total de resultados.

El total viaja en la cabecera `X-Total-Count` (y no en el cuerpo) para que los listados sigan devolviendo una
lista simple. Va expuesta por CORS para que el navegador pueda leerla.
"""
from datetime import UTC, datetime

from fastapi import Response

TOTAL_HEADER = "X-Total-Count"


def as_utc(value: datetime | None) -> datetime | None:
    """Una fecha sin zona horaria se toma como UTC (asyncpg no mezcla fechas con y sin zona)."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def page(limit: int, offset: int, *, max_limit: int) -> tuple[int, int]:
    return max(1, min(limit, max_limit)), max(0, offset)


def set_total(response: Response, total: int) -> None:
    response.headers[TOTAL_HEADER] = str(total)
