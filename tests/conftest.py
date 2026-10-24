import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings


@pytest.fixture
async def session():
    engine = create_async_engine(get_settings().database_url)
    try:
        conn = await engine.connect()
    except Exception:
        await engine.dispose()
        pytest.skip("PostgreSQL no disponible")
    trans = await conn.begin()
    async with AsyncSession(bind=conn, expire_on_commit=False) as s:
        yield s
    await trans.rollback()
    await conn.close()
    await engine.dispose()


@pytest.fixture
async def api(session, monkeypatch):
    """Cliente HTTP de la API con robotics-backend simulado (tokens tok-admin, tok-a y tok-b)."""
    import httpx
    import respx
    from httpx import ASGITransport

    from app.api.deps import get_queue
    from app.auth import backend
    from app.db.session import get_session
    from app.main import app
    from tests.helpers import BACKEND_USERS, FakeQueue

    monkeypatch.setenv("ROBOTICS_BACKEND_URL", "http://backend.test")
    get_settings.cache_clear()
    backend._client = None

    queue, state = FakeQueue(), {"down": False, "calls": 0}

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_queue] = lambda: queue

    def me(request):
        state["calls"] += 1
        if state["down"]:
            raise httpx.ConnectError("robotics-backend caido")
        user = BACKEND_USERS.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        return httpx.Response(200, json=user) if user else httpx.Response(401, json={"detail": "Unauthorized"})

    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://backend.test/api/auth/me").mock(side_effect=me)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://orq") as client:
            client.queue, client.state, client.mock = queue, state, mock
            yield client

    app.dependency_overrides.clear()
    await backend.close_auth_client()
    get_settings.cache_clear()
