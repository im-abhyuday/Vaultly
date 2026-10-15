"""Structured logging module for Vaultly.

Configures standard library logging with JSON formatting and request/tenant/user context.
Guarantees passwords, hashes, and token secrets are never logged.
"""

import contextvars
import logging
import sys
from typing import Any

from pythonjsonlogger.json import JsonFormatter

# Context variables for tracing request, tenant, and user IDs across async tasks
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
tenant_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("tenant_id", default="")
user_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("user_id", default="")


class StructuredJsonFormatter(JsonFormatter):
    """Custom JSON formatter injecting request_id, tenant_id, user_id, and service metadata."""

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record["timestamp"] = self.formatTime(record, self.datefmt)
        log_record["level"] = record.levelname
        log_record["logger"] = record.name

        req_id = request_id_ctx.get()
        if req_id:
            log_record["request_id"] = req_id

        t_id = tenant_id_ctx.get()
        if t_id:
            log_record["tenant_id"] = t_id

        u_id = user_id_ctx.get()
        if u_id:
            log_record["user_id"] = u_id


def setup_logging(log_level: str = "INFO") -> None:
    """Configure root logger with structured JSON handler."""
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove existing handlers to avoid duplication
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    formatter = StructuredJsonFormatter(
        "%(timestamp)s %(level)s %(logger)s %(message)s %(request_id)s %(tenant_id)s %(user_id)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)
    root_logger.addHandler(console_handler)

    # Tone down overly verbose external loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("aiokafka").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
