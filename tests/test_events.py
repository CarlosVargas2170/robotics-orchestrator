"""Eventos en vivo sobre PostgreSQL LISTEN/NOTIFY (necesita el Postgres de DATABASE_URL)."""
import asyncio
import json

import pytest

from app.core.config import get_settings
from app.events import EventBus, PostgresEventPublisher, asyncpg_dsn


@pytest.fixture
async def bus():
    bus = EventBus(asyncpg_dsn(get_settings().database_url))
    await bus.start()
    if bus._conn is None:
        await bus.stop()
        pytest.skip("PostgreSQL no disponible")
    yield bus
    await bus.stop()


@pytest.fixture
async def publisher():
    publisher = PostgresEventPublisher(asyncpg_dsn(get_settings().database_url))
    yield publisher
    await publisher.aclose()


def test_sqlalchemy_url_becomes_an_asyncpg_dsn():
    assert asyncpg_dsn("postgresql+asyncpg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"


async def test_a_published_event_reaches_every_subscriber(bus, publisher):
    first, second = bus.subscribe(), bus.subscribe()
    event = {"type": "alert_confirmed", "camera_id": "c1", "alert": "person_requesting_help_sos"}

    await publisher.publish(json.dumps(event))

    for queue in (first, second):
        assert json.loads(await asyncio.wait_for(queue.get(), 5)) == event


async def test_unsubscribed_clients_stop_receiving(bus, publisher):
    queue = bus.subscribe()
    bus.unsubscribe(queue)

    await publisher.publish('{"type": "detection"}')
    await asyncio.sleep(0.3)

    assert queue.empty()


async def test_an_event_over_the_notify_limit_is_dropped_without_failing(bus, publisher):
    queue = bus.subscribe()

    await publisher.publish("x" * 9000)  # NOTIFY admite 8000 bytes: se descarta, no revienta el pipeline
    await publisher.publish('{"type": "detection"}')

    assert json.loads(await asyncio.wait_for(queue.get(), 5)) == {"type": "detection"}
    assert queue.empty()


async def test_the_publisher_reconnects_if_its_connection_dies(bus, publisher):
    queue = bus.subscribe()
    await publisher.publish('{"n": 1}')
    await asyncio.wait_for(queue.get(), 5)

    publisher._conn.terminate()  # simula un corte de la conexion
    await publisher.publish('{"n": 2}')

    assert json.loads(await asyncio.wait_for(queue.get(), 5)) == {"n": 2}
