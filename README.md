# Robotics Orchestrator

Orquestador que consume clips de video de **Flussonic**, los envía a servicios de detección
(objetos, rostros, animales) y expone resultados a **Robot Control Interface** por REST y WebSocket.

Stack: Python 3.12, FastAPI, SQLAlchemy 2 (async) + PostgreSQL, Redis + Arq, ffmpeg.

## Arranque rápido (Docker)

```bash
cp .env.example .env      # ajusta FLUSSONIC_URL y las URLs de los detectores
docker compose up --build
```

API en http://localhost:8000 · docs en http://localhost:8000/docs

## Desarrollo local

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
docker compose up -d db redis
alembic revision --autogenerate -m "init" && alembic upgrade head
uvicorn app.main:app --reload
arq app.workers.tasks.WorkerSettings                # en otra terminal
pytest
```

Requiere `ffmpeg` en el PATH para extraer frames.

## Flujo

1. `POST /cameras` registra una cámara con su `stream_name` de Flussonic.
2. `POST /clips` (`camera_id`, `started_at`, `duration_s`) crea el clip y lo encola.
3. El worker descarga el clip (`/<stream>/archive-<from>-<dur>.mp4`), extrae frames a
   `FRAME_SAMPLE_FPS`, llama a los detectores en paralelo y guarda las `detections`.
4. Cada detección se publica en Redis y llega al frontend por `WS /ws/events`.

## Contrato de los servicios de detección

`POST {URL}/detect` (multipart, campo `image` JPEG) →

```json
{"detections": [{"label": "person", "confidence": 0.93, "bbox": [x, y, w, h]}]}
```

`bbox` normalizado 0..1 y opcional. Un detector se activa al definir su `DETECTOR_*_URL`.

## Estructura

```
app/api/        routers REST + WebSocket
app/ingest/     cliente Flussonic
app/pipeline/   extracción de frames y orquestación del clip
app/detectors/  adaptadores de servicios de IA
app/workers/    tareas Arq
app/db/         modelos y sesión
migrations/     Alembic
```
