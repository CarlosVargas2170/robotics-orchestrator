from datetime import UTC, datetime

import httpx
import respx

from app.core.config import Settings
from app.ingest.flussonic import FlussonicClient


def test_clip_url():
    client = FlussonicClient(Settings(flussonic_url="http://flu:8080/"))
    url = client.clip_url("cam1", datetime(2026, 1, 1, tzinfo=UTC), 10)
    assert url == "http://flu:8080/cam1/archive-1767225600-10.mp4"


@respx.mock
async def test_download_clip(tmp_path):
    respx.get("http://flu:8080/cam1/archive-1767225600-10.mp4").mock(
        return_value=httpx.Response(200, content=b"video-bytes")
    )
    client = FlussonicClient(Settings(flussonic_url="http://flu:8080"))
    dest = await client.download_clip(
        "cam1", datetime(2026, 1, 1, tzinfo=UTC), 10, tmp_path / "c.mp4"
    )
    assert dest.read_bytes() == b"video-bytes"
