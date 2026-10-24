from app.core.config import Settings
from app.detectors.base import Detector
from app.detectors.http import HttpDetector


def build_detectors(settings: Settings) -> list[Detector]:
    """Instancia solo los detectores con URL configurada."""
    urls = {
        "objects": settings.detector_objects_url,
        "faces": settings.detector_faces_url,
        "animals": settings.detector_animals_url,
    }
    return [
        HttpDetector(name, url, settings.detector_timeout_s) for name, url in urls.items() if url
    ]
