"""Worker process entrypoint for asynchronous Kafka document ingestion."""

import asyncio
import logging
import signal
from typing import Any

from app.core.config import get_settings
from app.core.logging import setup_logging

settings = get_settings()
setup_logging(log_level=settings.LOG_LEVEL)
logger = logging.getLogger("vaultly.worker")


class WorkerManager:
    """Manages worker lifecycle and graceful shutdown."""

    def __init__(self) -> None:
        self.is_running = True

    def stop(self, *_args: Any) -> None:
        logger.info("Received termination signal, shutting down workers gracefully...")
        self.is_running = False

    async def run(self) -> None:
        logger.info(
            "Vaultly Worker starting up",
            extra={
                "kafka_servers": settings.KAFKA_BOOTSTRAP_SERVERS,
                "environment": settings.ENVIRONMENT,
            },
        )

        # Loop until stopped
        while self.is_running:
            await asyncio.sleep(1)

        logger.info("Worker process terminated gracefully.")


async def main() -> None:
    manager = WorkerManager()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, manager.stop)
        except NotImplementedError:
            # Windows does not support add_signal_handler for all signals
            pass

    await manager.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Worker exiting.")
