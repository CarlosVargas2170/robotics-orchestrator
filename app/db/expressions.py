"""Expresiones SQL compartidas por los listados y el resumen del historial."""
from sqlalchemy import func

from app.db.models import Clip, Detection

# Instante real de una deteccion: inicio del clip + segundos dentro del clip
DETECTED_AT = Clip.started_at + func.make_interval(0, 0, 0, 0, 0, 0, Detection.offset_s)
