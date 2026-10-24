import asyncio
import signal

from app.core.config import get_settings
from app.workers.service import Worker


async def _main() -> None:
    worker = Worker(get_settings())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:  # Windows no admite add_signal_handler; Ctrl+C llega como KeyboardInterrupt
            pass
    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
