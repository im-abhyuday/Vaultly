"""Transactional outbox relay worker.

Queries unpublished outbox events using SELECT FOR UPDATE SKIP LOCKED,
publishes them to Kafka with acks=all, and records published_at in the database.
Safe to run in multiple concurrent worker replicas.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from aiokafka import AIOKafkaProducer
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.session import async_session_maker
from app.models.outbox import OutboxEvent

logger = logging.getLogger("vaultly.outbox_relay")
settings = get_settings()


class OutboxRelay:
    """Relay process delivering transactional outbox events to Kafka."""

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession] | None = None,
        producer: AIOKafkaProducer | None = None,
        batch_size: int = 50,
        poll_interval_ms: int | None = None,
    ) -> None:
        self.session_maker = session_maker or async_session_maker
        self.producer = producer
        self.batch_size = batch_size
        self.poll_interval = (poll_interval_ms or settings.OUTBOX_POLL_INTERVAL_MS) / 1000.0

    async def publish_batch(self, producer: AIOKafkaProducer) -> int:
        """Select a locked batch of unpublished events, publish to Kafka, and commit timestamps."""
        async with self.session_maker() as session:
            async with session.begin():
                stmt = (
                    select(OutboxEvent)
                    .where(OutboxEvent.published_at.is_(None))
                    .order_by(OutboxEvent.created_at.asc())
                    .limit(self.batch_size)
                    .with_for_update(skip_locked=True)
                )
                events = (await session.execute(stmt)).scalars().all()
                if not events:
                    return 0

                published_count = 0
                for event in events:
                    # Prepare message payload
                    payload_bytes = json.dumps(event.payload).encode("utf-8")
                    key_bytes = event.key.encode("utf-8")

                    # Convert headers dict to Kafka header tuples
                    headers_list: list[tuple[str, bytes]] = []
                    if event.headers:
                        for k, v in event.headers.items():
                            headers_list.append((k, str(v).encode("utf-8")))

                    try:
                        await producer.send_and_wait(
                            event.topic,
                            value=payload_bytes,
                            key=key_bytes,
                            headers=headers_list,
                        )
                        event.published_at = datetime.now(UTC)
                        event.last_error = None
                        published_count += 1
                    except Exception as err:
                        event.attempts += 1
                        event.last_error = f"Kafka publish failure: {err}"
                        logger.error(
                            f"Failed to publish outbox event {event.id} to topic {event.topic}: {err}"
                        )

                return published_count

    async def purge_old_events(self, retention_days: int | None = None) -> int:
        """Purge successfully published outbox events older than retention cutoff."""
        days = retention_days if retention_days is not None else settings.OUTBOX_RETENTION_DAYS
        cutoff = datetime.now(UTC) - timedelta(days=days)

        async with self.session_maker() as session:
            async with session.begin():
                del_stmt = delete(OutboxEvent).where(
                    OutboxEvent.published_at.is_not(None),
                    OutboxEvent.published_at < cutoff,
                )
                res = await session.execute(del_stmt)
                purged = int(getattr(res, "rowcount", 0) or 0)
                if purged > 0:
                    logger.info(f"Purged {purged} old published outbox events (retention={days} days).")
                return purged

    async def run(self, stop_event: asyncio.Event, producer: AIOKafkaProducer) -> None:
        """Continuous relay poll loop."""
        logger.info("Outbox relay worker started.")
        purge_counter = 0

        while not stop_event.is_set():
            try:
                published = await self.publish_batch(producer)
                if published == 0:
                    await asyncio.sleep(self.poll_interval)

                purge_counter += 1
                if purge_counter >= 1000:
                    purge_counter = 0
                    await self.purge_old_events()
            except asyncio.CancelledError:
                break
            except Exception as err:
                logger.error(f"Error in outbox relay loop: {err}", exc_info=True)
                await asyncio.sleep(self.poll_interval * 2)

        logger.info("Outbox relay worker stopped.")
