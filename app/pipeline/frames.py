import asyncio
import tempfile
from pathlib import Path


async def extract_frames(clip_path: Path, fps: float) -> list[tuple[float, bytes]]:
    """Extrae frames JPEG con ffmpeg a `fps` cuadros por segundo.

    Devuelve [(offset_s, jpeg_bytes)]. Requiere `ffmpeg` en el PATH.
    """
    with tempfile.TemporaryDirectory() as tmp:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-i", str(clip_path),
            "-vf", f"fps={fps}", "-q:v", "3", f"{tmp}/f_%05d.jpg",
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg fallo: {err.decode(errors='replace')}")
        files = sorted(Path(tmp).glob("f_*.jpg"))
        return [(i / fps, f.read_bytes()) for i, f in enumerate(files)]
