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
| `admin` | Todas las cámaras, clips, detecciones y alertas. Da de alta cámaras y asigna su dueño (`owner_user_id`). Gestiona servicios y permisos. |
| `cliente` | Solo las cámaras que tiene asignadas y sus clips, detecciones y alertas. Puede pedir clips y atender alertas de ellas. |
| `operator` | **Sin acceso** (403): es el rol del control de robots (botones, audios, voz), otro producto. Lo mismo vale para cualquier rol que no sea `admin` o `cliente`. |

El rol `cliente` se crea en `robotics-backend` (migración `0015`) y ese backend le impide usar el control de robots.
El orquestador comprueba el rol en **cada** petición, también cuando el token sale de la caché. En el WebSocket,
un rol sin acceso cierra con el código `4403`.

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

## Alertas

Cuando una alerta del servicio se confirma (ver «Eventos de `WS /ws/events`»), además del evento en vivo
queda registrada en la tabla `alerts`: **una fila por alerta, no por frame**. Así el operador que abre la
pantalla más tarde ve lo que sigue pendiente, aunque no estuviera conectado cuando ocurrió.

| Endpoint | Para qué |
|---|---|
| `GET /alerts` | Alertas de tus cámaras (el admin ve todas), de la más reciente a la más antigua. Filtros: `acknowledged` (`false` = pendientes), `camera_id`, `alert`, `since`, `until`, `limit` (máx. 200), `offset` |
| `GET /alerts/{id}` | Una alerta (404 si no es de tus cámaras). Trae `video_available`: si el video del clip sigue guardado |
| `GET /alerts/{id}/video` | El MP4 del clip donde se confirmó la alerta (admite `Range`, para saltar dentro del video). `first_offset_s` y `offset_s` de la alerta son segundos dentro de este video. 404 si no es de tus cámaras o si ya no está guardado (se conserva `CLIP_RETENTION_DAYS`, 30 por defecto). Cada descarga queda en la auditoría (`alert.video`). Solo se sirven archivos dentro de `CLIPS_DIR`; la API y el worker deben compartir esa carpeta |
| `POST /alerts/{id}/acknowledge` | Marca la alerta como atendida, con una nota opcional (máx. 500). Si ya estaba atendida no se sobrescribe quién lo hizo primero |

Cada alerta incluye `robot_code`, el tipo (`alert`), la persona (`track_id`), el instante real en que empezó
(`started_at`), los frames que la sostuvieron, la confianza máxima y quién y cuándo la atendió.

Dos eventos de `WS /ws/events` se relacionan con ellas: `alert_confirmed` (lleva `alert_id`) y
`alert_acknowledged`, que se emite al atenderla para que las demás pantallas abiertas la quiten de su lista.

Los eventos se publican **después** de guardar los datos en la BD, de modo que al recibir uno ya se puede consultar.
Si un clip se reprocesa porque un worker cayó a mitad, se descartan las detecciones y alertas parciales del
intento anterior para no duplicarlas.

## Historial

Todos los listados devuelven una lista simple, de lo más reciente a lo más antiguo, solo de los robots del usuario
(el admin ve todos). El **total de resultados, sin paginar**, va en la cabecera `X-Total-Count` (expuesta por CORS para
que el navegador la lea). Las fechas sin zona horaria se toman como UTC; `since` se incluye y `until` se excluye.

| Endpoint | Filtros | Cada fila incluye |
|---|---|---|
| `GET /detections` | `since`, `until` (instante real de la detección), `camera_id`, `clip_id`, `detector_id`, `kind` (tipo de servicio: `persons`, `faces`...), `label`, `alert`, `alert_confirmed`, `min_confidence`, `limit` (máx. 500), `offset` | `detector_name`, `detector_kind`, `robot_code`, `camera_id`, `detected_at` (inicio del clip + `offset_s`) |
| `GET /clips` | `since`, `until` (inicio del clip), `camera_id`, `status` (`done`, `failed`, `no_data`...), `source`, `limit` (máx. 200), `offset` | `robot_code`, estado y error |
| `GET /alerts` | `acknowledged`, `camera_id`, `alert`, `kind`, `since`, `until`, `limit` (máx. 200), `offset` | `robot_code`, `detector_name`, `detector_kind`, `video_available` |

### Resumen para pantallas y gráficos

`GET /stats/summary?since=...&until=...&tz=America/La_Paz&camera_id=...` devuelve, en una sola llamada y solo de los
robots del usuario: las **alertas** (total, pendientes, por día, por tipo y por robot), las **detecciones** (total, por
servicio y por día) y los **clips** (total y por estado, con todos los estados presentes aunque valgan 0).

- Por defecto cuenta los últimos 7 días; el rango máximo es de 366 días.
- `tz` es la zona horaria con la que se agrupa por día (por defecto `UTC`). Una alerta a las 02:00 UTC del día 11 cae en
  el día 10 con `America/La_Paz`. La zona la valida PostgreSQL, que es quien agrupa.

### Rendimiento

El instante real de una detección es `clip.started_at + offset_s`, una expresión sin índice, así que filtrar y ordenar
por fecha recorre `detections`. Medido con 576.000 detecciones (~2 días muy intensos de un robot): el total de un día tarda
~0,2 s, una página de 100 ~0,26 s y el resumen de 7 días ~0,56 s. Crece de forma lineal; con decenas de millones de filas
conviene guardar `detected_at` y `camera_id` en `detections` con un índice, y definir cuánto tiempo se conservan las
detecciones.

## Gestión de robots y servicios

### Robots (`/cameras`)

| Endpoint | Quién | Para qué |
|---|---|---|
| `GET /cameras` | Dueño (los suyos) o admin (todos) | Lista con `owner_user_id` (el admin ve los de un usuario), `enabled`, `robot_code`, `q` (busca en nombre, stream y código), `limit` (máx. 200) y `offset`. El total va en `X-Total-Count`. |
| `GET /cameras/{id}` | Dueño o admin | Un robot (404 si no es tuyo) |
| `POST /cameras`, `PATCH /cameras/{id}` | Admin | Alta, edición y reasignación de dueño |
| `DELETE /cameras/{id}` | Admin | Solo robots que **nunca procesaron nada**. Con historial responde 409: se desactiva con `PATCH {"enabled": false}`, que conserva el historial y detiene la ingesta. |

### Validación contra robotics-backend

Las dos bases de datos son independientes y no hay claves foráneas entre ellas. Para no guardar registros fantasma,
al **registrar o cambiar** el dueño (`owner_user_id`) o el `robot_code`, y al **dar permiso** de un servicio a un usuario,
el orquestador pregunta al backend (`GET /api/auth/users` y `GET /api/robots`) **con el token del propio admin**, sin
credenciales aparte. Responde **422** con el motivo si:

- el usuario no existe en el backend, o está desactivado;
- el usuario tiene rol `operator` (solo `admin` y `cliente` usan este producto);
- el robot no existe en el backend (uno inactivo sí vale).

Si el backend no responde se rechaza con **503**: es preferible reintentar que guardar sin validar. Solo se consulta
cuando algo cambia (editar la ubicación, repetir el mismo dueño o dejar el robot sin dueño no preguntan nada).
Suspender o quitar un permiso **no** exige que el usuario exista, para poder limpiar a quien ya se borró del backend.

### Servicios

- `GET /detectors/adapters` (admin): los tipos de servicio que el sistema sabe leer, con su descripción, sus rutas y si
  tienen chequeo de salud. Es la lista de valores válidos de `adapter` al registrar un servicio.
- `GET /users/{user_id}/detectors` (admin): **todos** los servicios indicando, para ese usuario, si puede usarlos
  (`allowed`), si el permiso está suspendido (`suspended`), si el servicio está habilitado (`enabled`) y en qué robots
  suyos está encendido (`active_on`). No valida que el usuario exista, para poder revisar a uno ya borrado.

Dar permiso a un usuario **no activa** el servicio en sus robots: se activa robot por robot
(`PUT /cameras/{id}/detectors/{detector_id}`), porque cada servicio consume recursos y recibe imágenes.
