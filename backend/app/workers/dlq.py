"""Dead-Letter Queue (DLQ) publisher for poison pills, permanent failures, and exhausted retries."""

import logging
from datetime import UTC, datetime

from aiokafka import AIOKafkaProducer

logger = logging.getLogger("vaultly.dlq")


async def publish_to_dlq(
    producer: AIOKafkaProducer,
    dlq_topic: str,
    raw_message_bytes: bytes,
    key_bytes: bytes | None,
    stage: str,
    error_code: str,
    error_message: str,
    attempts: int,
    source_topic: str,
    source_partition: int,
    source_offset: int,
) -> None:
    """Publish failed message to DLQ topic with diagnostic headers.

    If DLQ publishing fails, the consumer must NOT commit the offset.
    """
    failed_at = datetime.now(UTC).isoformat()
    headers: list[tuple[str, bytes]] = [
        ("error_code", error_code.encode("utf-8")),
        ("error_message", error_message.encode("utf-8")),
        ("attempts", str(attempts).encode("utf-8")),
        ("stage", stage.encode("utf-8")),
        ("failed_at", failed_at.encode("utf-8")),
        ("source_topic", source_topic.encode("utf-8")),
        ("source_partition", str(source_partition).encode("utf-8")),
        ("source_offset", str(source_offset).encode("utf-8")),
    ]

    logger.warning(
        f"Publishing message to DLQ '{dlq_topic}' (stage={stage}, error_code={error_code}, attempts={attempts})"
    )

    await producer.send_and_wait(
        dlq_topic,
        value=raw_message_bytes,
        key=key_bytes,
        headers=headers,
    )
