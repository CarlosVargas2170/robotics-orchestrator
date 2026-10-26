import asyncio

import httpx
import pytest
import respx

from app.detectors.base import FrameContext
from app.detectors.http import HttpDetector

CTX = FrameContext(robot_id="SERBOT01", timestamp=1729742400.15)

# Respuestas tomadas del swagger del Servicio 1 (deteccion de personas v2.1.0)
PERSON_WALKING = {
    "service": "person_detection", "robot_id": "SERBOT01", "timestamp": 1729742400.15,
    "detected": True, "persons_count": 1, "has_vulnerable_person": False, "last_incident_evidence": None,
    "detections": [{
        "person_index": 1, "track_id": 3, "confidence": 0.925, "posture": "standing", "alert": None,
        "bbox": {"x1": 450, "y1": 180, "x2": 640, "y2": 690},
    }],
}
PERSON_FALLEN = {
    "service": "person_detection", "detected": True, "persons_count": 1, "has_vulnerable_person": True,
    "last_incident_evidence": {"filename": "evidencia.mp4", "url": "/api/v1/incidents/video/evidencia.mp4"},
    "detections": [{
        "track_id": 3, "confidence": 0.892, "posture": "lying_down",
        "alert": "critical_unresponsive_person_on_ground",
        "bbox": {"x1": 210, "y1": 520, "x2": 830, "y2": 700},
    }],
}


@respx.mock
async def test_generic_adapter_parses_contract():
    respx.post("http://det:9000/detect").mock(
        return_value=httpx.Response(
            200, json={"detections": [{"label": "dog", "confidence": 0.9, "bbox": [1, 2, 3, 4]}]}
        )
    )
    results = await HttpDetector("animals", "http://det:9000", 5).detect(b"jpeg", CTX)
    assert [(r.label, r.bbox) for r in results] == [("dog", [1, 2, 3, 4])]


@respx.mock
async def test_person_adapter_sends_frame_with_camera_context():
    route = respx.post("http://person:8001/api/v1/analyze/frame").mock(
        return_value=httpx.Response(200, json=PERSON_WALKING)
    )
    detector = HttpDetector("personas", "http://person:8001", 5, adapter="person_detection_v1")
    results = await detector.detect(b"jpeg", CTX)

    body = route.calls.last.request.content
    assert b'name="file"' in body and b'name="robot_id"' in body and b"SERBOT01" in body
    assert b"1729742400.15" in body
    assert len(results) == 1
    r = results[0]
    assert (r.label, r.confidence, r.track_id, r.alert) == ("person", 0.925, 3, None)
    assert r.bbox == [450, 180, 640, 690]


@respx.mock
async def test_adapters_send_camera_and_client_when_known():
    ctx = FrameContext("SERBOT01", 1729742400.15, camera_id="cam-uuid-1", client_id="client-uuid-1")
    person = respx.post("http://person:8001/api/v1/analyze/frame").mock(
        return_value=httpx.Response(200, json=PERSON_WALKING))
    generic = respx.post("http://det:9000/detect").mock(return_value=httpx.Response(200, json={"detections": []}))

    await HttpDetector("personas", "http://person:8001", 5, adapter="person_detection_v1").detect(b"jpeg", ctx)
    await HttpDetector("animals", "http://det:9000", 5).detect(b"jpeg", ctx)

    for route in (person, generic):
        body = route.calls.last.request.content
        assert b'name="camera_id"' in body and b"cam-uuid-1" in body
        assert b'name="client_id"' in body and b"client-uuid-1" in body


@respx.mock
async def test_adapters_omit_client_without_owner():
    route = respx.post("http://person:8001/api/v1/analyze/frame").mock(
        return_value=httpx.Response(200, json=PERSON_WALKING))
    ctx = FrameContext("SERBOT01", 1729742400.15, camera_id="cam-uuid-1")  # camara sin dueño
    await HttpDetector("personas", "http://person:8001", 5, adapter="person_detection_v1").detect(b"jpeg", ctx)

    body = route.calls.last.request.content
    assert b'name="camera_id"' in body
    assert b'name="client_id"' not in body


@respx.mock
async def test_person_adapter_keeps_alert_and_incident_evidence():
    respx.post("http://person:8001/api/v1/analyze/frame").mock(
        return_value=httpx.Response(200, json=PERSON_FALLEN)
    )
    detector = HttpDetector("personas", "http://person:8001", 5, adapter="person_detection_v1")
    (r,) = await detector.detect(b"jpeg", CTX)

    assert r.alert == "critical_unresponsive_person_on_ground"
    assert r.raw["posture"] == "lying_down"
    assert r.raw["incident_evidence"]["filename"] == "evidencia.mp4"


@respx.mock
async def test_person_adapter_returns_nothing_when_no_person():
    respx.post("http://person:8001/api/v1/analyze/frame").mock(
        return_value=httpx.Response(200, json={"service": "person_detection", "detected": False, "persons_count": 0})
    )
    detector = HttpDetector("personas", "http://person:8001", 5, adapter="person_detection_v1")
    assert await detector.detect(b"jpeg", CTX) == []


def test_unknown_adapter_is_rejected():
    with pytest.raises(ValueError, match="Adaptador desconocido"):
        HttpDetector("x", "http://x", 5, adapter="nope")


@respx.mock
async def test_concurrent_requests_to_a_service_are_limited():
    in_flight = peak = 0

    async def slow(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(200, json={"detections": []})

    respx.post("http://det:9000/detect").mock(side_effect=slow)
    detector = HttpDetector("objects", "http://det:9000", 5, max_concurrency=2)
    await asyncio.gather(*(detector.detect(b"x", CTX) for _ in range(8)))

    assert peak == 2
