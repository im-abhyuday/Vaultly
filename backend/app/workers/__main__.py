"""Worker CLI entrypoint: python -m app.workers --role relay|parse|index|all.

Handles graceful SIGTERM/SIGINT shutdown, ensures Kafka topics exist,
preloads embedding models once per process, and coordinates background asyncio tasks.
"""

import argparse
import asyncio
import logging
import signal
import sys
from typing import Any

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import engine
from app.services.embedding import get_embedding_provider
from app.workers.index_consumer import IndexConsumer
from app.workers.infra import create_consumer, create_producer, ensure_topics, get_topic_name
from app.workers.parse_consumer import ParseConsumer
from app.workers.relay import OutboxRelay

settings = get_settings()
setup_logging()
logger = logging.getLogger("vaultly.worker")


async def main(role: str) -> None:
    """Run worker services according to the requested role."""
    logger.info(f"Starting Vaultly worker with role '{role}' in environment '{settings.ENVIRONMENT}'.")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Graceful shutdown signal handlers
    def _signal_handler() -> None:
        logger.info("Received termination signal, initiating graceful worker shutdown...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows signal fallback
            signal.signal(sig, lambda *_: _signal_handler())

    # Step 1: Ensure required topics exist in Kafka broker
    await ensure_topics()

    # Step 2: Initialize producer
    producer = create_producer()
    await producer.start()

    # Step 3: Preload embedding model if index role is active
    if role in ("index", "all"):
        logger.info(
            f"Preloading embedding provider '{settings.EMBEDDING_PROVIDER}' (model: {settings.EMBEDDING_MODEL})..."
        )
        get_embedding_provider()

    tasks: list[asyncio.Task[Any]] = []
    parse_consumer = None
    index_consumer = None

    try:
        # Start Outbox Relay
        if role in ("relay", "all"):
            relay = OutboxRelay()
            tasks.append(asyncio.create_task(relay.run(stop_event, producer), name="relay"))

        # Start Parse Consumer
        if role in ("parse", "all"):
            uploaded_topic = get_topic_name("document.uploaded")
            parse_group = f"{settings.KAFKA_CONSUMER_GROUP_PREFIX}-parse"
            parse_consumer = create_consumer(
                topic=uploaded_topic,
                group_id=parse_group,
                max_poll_records=1,
            )
            await parse_consumer.start()
            consumer_service = ParseConsumer(consumer=parse_consumer, producer=producer)
            tasks.append(asyncio.create_task(consumer_service.run(stop_event), name="parse_consumer"))

        # Start Index Consumer
        if role in ("index", "all"):
            parsed_topic = get_topic_name("document.parsed")
            index_group = f"{settings.KAFKA_CONSUMER_GROUP_PREFIX}-index"
            index_consumer = create_consumer(
                topic=parsed_topic,
                group_id=index_group,
                max_poll_records=1,
            )
            await index_consumer.start()
            index_service = IndexConsumer(consumer=index_consumer, producer=producer)
            tasks.append(asyncio.create_task(index_service.run(stop_event), name="index_consumer"))

        logger.info(f"Worker tasks started: {[t.get_name() for t in tasks]}. Awaiting events...")

        # Wait until stop_event is triggered
        await stop_event.wait()

    finally:
        logger.info("Stopping Kafka consumers and waiting for in-flight tasks...")
        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if parse_consumer:
            await parse_consumer.stop()
        if index_consumer:
            await index_consumer.stop()

        await producer.stop()
        await engine.dispose()
        logger.info("All worker components cleanly shut down.")


def main_cli() -> None:
    """CLI entrypoint for worker daemon."""
    parser = argparse.ArgumentParser(description="Vaultly Ingestion Pipeline Worker")
    parser.add_argument(
        "--role",
        choices=["relay", "parse", "index", "all"],
        default="all",
        help="Worker role to execute (default: all)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(args.role))
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)


if __name__ == "__main__":
    main_cli()
