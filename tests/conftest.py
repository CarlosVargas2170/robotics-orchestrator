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

    from app.auth import backend
    from app.db.session import get_session
    from app.main import app
    from tests.helpers import BACKEND_USERS, DIRECTORY_ROBOTS, DIRECTORY_USERS, FakePublisher

    monkeypatch.setenv("ROBOTICS_BACKEND_URL", "http://backend.test")
    get_settings.cache_clear()
    backend._client = None

    state = {"down": False, "calls": 0, "directory_down": False, "directory": []}

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    publisher = FakePublisher()
    app.state.publisher = publisher

    def me(request):
        state["calls"] += 1
        if state["down"]:
            raise httpx.ConnectError("robotics-backend caido")
        user = BACKEND_USERS.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        return httpx.Response(200, json=user) if user else httpx.Response(401, json={"detail": "Unauthorized"})

    def directory(payload):
        def handler(request):
            # (ruta, cabecera de autorizacion): permite comprobar que se consulto con el token del admin
            state["directory"].append((request.url.path, request.headers.get("authorization")))
            if state["directory_down"]:
                raise httpx.ConnectError("directorio caido")
            return httpx.Response(200, json=payload)
        return handler

    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://backend.test/api/auth/me").mock(side_effect=me)
        mock.get("http://backend.test/api/auth/users").mock(side_effect=directory(DIRECTORY_USERS))
        mock.get("http://backend.test/api/robots").mock(side_effect=directory(DIRECTORY_ROBOTS))
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://orq") as client:
            client.state, client.mock, client.publisher = state, mock, publisher
            yield client

    app.dependency_overrides.clear()
    del app.state.publisher
    await backend.close_auth_client()
    get_settings.cache_clear()
