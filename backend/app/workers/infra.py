"""Kafka infrastructure helpers: topic provisioning, producer and consumer factories."""

import asyncio
import logging

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError

from app.core.config import get_settings

logger = logging.getLogger("vaultly.kafka")
settings = get_settings()

PIPELINE_TOPICS = [
    "document.uploaded",
    "document.parsed",
    "document.uploaded.dlq",
    "document.parsed.dlq",
]


def get_topic_name(base_topic: str, prefix: str | None = None) -> str:
    """Format full topic name including optional topic prefix."""
    pfx = prefix if prefix is not None else settings.KAFKA_TOPIC_PREFIX
    if pfx:
        return f"{pfx}.{base_topic}"
    return base_topic


async def ensure_topics(
    bootstrap_servers: str | None = None,
    prefix: str | None = None,
    num_partitions: int = 3,
    replication_factor: int = 1,
    max_retries: int = 10,
    retry_delay: float = 1.0,
) -> list[str]:
    """Idempotently create required pipeline topics with retry backoff."""
    servers = bootstrap_servers or settings.KAFKA_BOOTSTRAP_SERVERS
    topic_names = [get_topic_name(t, prefix) for t in PIPELINE_TOPICS]

    for attempt in range(1, max_retries + 1):
        admin = AIOKafkaAdminClient(bootstrap_servers=servers)
        try:
            await admin.start()
            try:
                new_topics = [
                    NewTopic(
                        name=t_name,
                        num_partitions=num_partitions,
                        replication_factor=replication_factor,
                    )
                    for t_name in topic_names
                ]
                await admin.create_topics(new_topics)
                logger.info(f"Created Kafka topics: {topic_names}")
                return topic_names
            except TopicAlreadyExistsError:
                logger.info(f"Kafka topics already exist: {topic_names}")
                return topic_names
            except Exception as err:
                if "TopicExistsError" in str(err) or "TopicAlreadyExists" in str(err):
                    return topic_names
                raise
            finally:
                await admin.close()
        except Exception as err:
            logger.warning(
                f"Kafka admin connection attempt {attempt}/{max_retries} failed: {err}. Retrying in {retry_delay}s..."
            )
            if attempt == max_retries:
                logger.error("Failed to connect to Kafka broker to provision topics.")
                raise
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, 10.0)

    return topic_names


def create_producer(bootstrap_servers: str | None = None) -> AIOKafkaProducer:
    """Create configured AIOKafkaProducer with acks=all and idempotence enabled."""
    servers = bootstrap_servers or settings.KAFKA_BOOTSTRAP_SERVERS
    return AIOKafkaProducer(
        bootstrap_servers=servers,
        enable_idempotence=True,
        acks="all",
    )


def create_consumer(
    topic: str,
    group_id: str,
    bootstrap_servers: str | None = None,
    max_poll_records: int = 1,
    max_poll_interval_ms: int = 600000,  # 10 minutes
    auto_offset_reset: str = "earliest",
) -> AIOKafkaConsumer:
    """Create configured AIOKafkaConsumer with manual commit and generous poll interval."""
    servers = bootstrap_servers or settings.KAFKA_BOOTSTRAP_SERVERS
    return AIOKafkaConsumer(
        topic,
        bootstrap_servers=servers,
        group_id=group_id,
        enable_auto_commit=False,
        auto_offset_reset=auto_offset_reset,
        max_poll_records=max_poll_records,
        max_poll_interval_ms=max_poll_interval_ms,
    )
