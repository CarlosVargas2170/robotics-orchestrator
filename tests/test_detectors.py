import httpx
import respx

from app.detectors.http import HttpDetector


@respx.mock
async def test_http_detector_parses_contract():
    respx.post("http://det:9000/detect").mock(
        return_value=httpx.Response(
            200, json={"detections": [{"label": "person", "confidence": 0.9, "bbox": [0.1, 0.2, 0.3, 0.4]}]}
        )
    )
    detector = HttpDetector("objects", "http://det:9000", 5)
    results = await detector.detect(b"jpeg")
    assert len(results) == 1
    assert results[0].label == "person"
    assert results[0].bbox == [0.1, 0.2, 0.3, 0.4]
