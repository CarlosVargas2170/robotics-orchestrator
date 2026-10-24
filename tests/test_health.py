from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers import health


def test_health():
    app = FastAPI()
    app.include_router(health.router)
    assert TestClient(app).get("/health").json() == {"status": "ok"}
