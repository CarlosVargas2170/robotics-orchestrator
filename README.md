# Robotics Orchestrator

Orquestador que consume clips de video de **Flussonic**, los envía a servicios de detección
(objetos, rostros, animales) y expone resultados a **Robot Control Interface** por REST y WebSocket.

Stack: Python 3.12, FastAPI, SQLAlchemy 2 (async) + PostgreSQL, ffmpeg. PostgreSQL es también la cola de trabajo
y el canal de eventos en vivo: no hace falta Redis.

## Arranque rápido (Docker)

```bash
cp .env.example .env      # ajusta FLUSSONIC_URL y las URLs de los detectores
docker compose up --build
```

API en http://localhost:8000 · docs en http://localhost:8000/docs

## Desarrollo local

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
docker compose up -d db
alembic upgrade head
uvicorn app.main:app --reload
python -m app.workers                                # el worker, en otra terminal
pytest
```

Requiere `ffmpeg` en el PATH para extraer frames.

## Flujo

1. `POST /cameras` registra una cámara con su `stream_name` de Flussonic.
2. `POST /clips` (`camera_id`, `started_at`, `duration_s`) crea el clip, que queda `pending` en la cola.
3. El worker descarga el clip (`/<stream>/archive-<from>-<dur>.mp4`), extrae frames a
   `FRAME_SAMPLE_FPS`, llama a los detectores en paralelo y guarda las `detections`.
4. Cada detección se publica con `NOTIFY` de PostgreSQL y llega al frontend por `WS /ws/events`.

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
app/workers/    worker: consume la cola y ejecuta las tareas periódicas
app/queue.py    cola de trabajo sobre la tabla clips
app/events.py   eventos en vivo (LISTEN/NOTIFY)
app/db/         modelos y sesión
migrations/     Alembic
```

## Eventos de `WS /ws/events`

Cada mensaje es un JSON con `type`:

- `detection`: una detección individual (`detector`, `kind`, `label`, `confidence`, `offset_s`, `bbox`,
  `track_id`, `alert`). `alert` es la alerta **cruda** del servicio y puede ser un falso positivo.
- `alert_confirmed`: se emite **una vez** cuando la misma persona (`track_id`) mantiene la misma alerta
  con `confidence >= alert_min_confidence` durante `alert_min_frames` frames consecutivos. Es la que
  debe usar el frontend para avisar al operador.

Los umbrales son por servicio (`detectors.alert_min_confidence`, por defecto 0.7, y
`detectors.alert_min_frames`, por defecto 3). `detectors.max_concurrency` (por defecto 2) limita las
peticiones simultáneas a ese servicio por proceso del worker. En la BD, `detections.alert_confirmed`
marca toda la racha; `GET /detections?alert_confirmed=true` devuelve solo las alertas confirmadas.

## Autenticación

El orquestador no gestiona usuarios: valida el access token que emite `robotics-backend`
(`POST {ROBOTICS_BACKEND_URL}/api/auth/login`) llamando a `GET /api/auth/me`, con caché de 45 s.
Todos los endpoints, salvo `GET /health`, exigen `Authorization: Bearer <access_token>`.
El `PANEL_TOKEN` compartido del backend **no** sirve aquí.

| Rol (viene del backend) | Qué ve y qué puede hacer |
|---|---|
| `admin` | Todas las cámaras, clips y detecciones. Da de alta cámaras y asigna su dueño (`owner_user_id`). |
| cualquier otro | Solo las cámaras que tiene asignadas y sus clips y detecciones. Puede pedir clips de ellas. |

Las cámaras ajenas responden 404, igual que si no existieran. `ROBOTICS_BACKEND_URL` es obligatoria;
sin ella, o si el backend no responde, la API responde 503.

**WebSocket:** `WS /ws/events` exige que el primer mensaje sea `{"token": "<access_token>"}` (si no llega
en 5 s, cierra con código 4401). Solo envía eventos de las cámaras del usuario. El token se revalida cada
minuto: al vencer (15 min en el backend) el servidor cierra con 4401 y el cliente debe renovarlo con
`POST /api/auth/refresh` y reconectar.

## Gestión de servicios de IA

Los servicios (personas, rostros, animales, objetos) se registran por API, sin tocar `.env` ni la BD.
Cada clip se envía solo a los servicios que cumplen las tres condiciones: están **activados en la cámara**,
**habilitados globalmente** y el **dueño de la cámara tiene permiso**.

| Endpoint | Quién | Para qué |
|---|---|---|
| `POST /detectors` · `GET /detectors` · `PATCH`/`DELETE /detectors/{id}` | admin | Registrar y editar servicios (URL, adaptador, umbrales) |
| `GET /detectors/{id}/health` | admin | Comprobar que el servicio está en línea |
| `PUT`/`DELETE /detectors/{id}/access/{user_id}` · `GET .../access` | admin | Dar, suspender (`enabled: false`) o quitar el permiso de un usuario |
| `GET /detectors/available` | cualquiera | Servicios que el usuario puede activar (sin exponer la URL interna) |
| `PATCH /cameras/{id}` | admin | Editar una cámara o reasignarla a otro usuario (`owner_user_id`) |
| `PUT`/`DELETE /cameras/{id}/detectors/{detector_id}` · `GET .../detectors` | dueño o admin | Activar, apagar o quitar un servicio en la cámara |

Flujo para dar de alta el servicio de personas:

```
1. POST /detectors                      {"name": "Personas", "kind": "persons", "adapter": "person_detection_v1",
                                         "base_url": "http://IP:8001"}
2. GET  /detectors/{id}/health          -> {"online": true, ...}
3. PUT  /detectors/{id}/access/{user}   (el id del usuario en robotics-backend)
4. PUT  /cameras/{camera}/detectors/{id}
```

`base_url` solo admite `http`/`https`, sin credenciales, y rechaza direcciones de metadatos (169.254.x.x). Un
servicio con detecciones guardadas no se elimina (409): se deshabilita con `PATCH {"enabled": false}`.
No se comprueba que el `user_id` de un permiso exista en robotics-backend.

## Ingesta automática

El worker (`python -m app.workers`) pide solo los clips a Flussonic. Cada 30 s (a los :00 y :30) revisa las
cámaras con `auto_ingest = true` y, si toca, crea un clip (`source = SCHEDULE`) que queda en cola.

Se configura por cámara con `PATCH /cameras/{id}` (solo admin):

| Campo | Significado |
|---|---|
| `auto_ingest` | Activa la ingesta automática (por defecto `false`) |
| `ingest_duration_s` | Segundos de video de cada tramo (5 a 300, por defecto 30) |
| `ingest_interval_s` | Cada cuántos segundos se pide un tramo (no puede ser menor que la duración) |

`interval == duration` da cobertura continua; `interval > duration` es muestreo (p. ej. 15 s de cada minuto).
Solo se piden tramos con `INGEST_LAG_S` (20 s) de antigüedad, y tras una caída larga no se recupera más de
`INGEST_MAX_BACKLOG_S` (300 s) de atraso. Una cámara sin servicios activos no descarga video.

- **Orden por cámara:** los clips de una cámara se procesan de uno en uno y en orden; si hay uno anterior sin
  terminar, el siguiente espera (un clip atascado más de 10 min no bloquea).
- **Sin archivo:** si Flussonic no tiene el tramo (p. ej. robot apagado) el clip queda en `no_data`, no en `failed`.
- **Videos:** se borran al procesarse, salvo los que tienen una alerta confirmada (evidencia) o si
  `KEEP_CLIPS=true`. Cada noche (03:00) se borran los que superan `CLIP_RETENTION_DAYS` (30); los registros se conservan.

## Cola de trabajo sobre PostgreSQL

No hay un servicio de cola aparte: la tabla `clips` es la cola y un clip `pending` es un trabajo en espera.

- Cada worker tiene `WORKER_CONCURRENCY` (4) consumidores. Toman clips con `FOR UPDATE SKIP LOCKED`, así que
  se pueden lanzar varios procesos worker sin que dos tomen el mismo clip.
- Al crearse un clip se avisa con `NOTIFY`; si el aviso se pierde, cada `WORKER_POLL_INTERVAL_S` (2 s) se consulta igual.
- Cada intento incrementa `clips.attempts`. Si un worker cae a mitad (el clip lleva `CLIP_STALE_S`, 600 s, sin
  avanzar) el clip se reencola, y tras `MAX_ATTEMPTS` (3) intentos pasa a `failed`.
- Al apagar el worker (Ctrl+C o `SIGTERM`) termina los clips en curso, esperando hasta `WORKER_SHUTDOWN_S` (60 s).
- Los eventos en vivo usan `LISTEN/NOTIFY`, que necesita conexión **directa** a PostgreSQL (no a través de
  pgbouncer ni de un pooler en modo transacción). Son efímeros: quien no está conectado no los recibe, pero los
  datos siempre están en `GET /detections`. Cada evento admite hasta 8 KB.

**Usar Redis más adelante:** la cola y los eventos están aislados en `app/queue.py` y `app/events.py`. Pasar a
Redis significa implementar esas dos piezas (y el arranque del worker) con Redis; el resto del sistema no cambia.
