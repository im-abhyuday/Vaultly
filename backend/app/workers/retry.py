"""Exponential backoff with full jitter for in-process transient error retries."""

import random


def calculate_backoff_delay(
    attempt: int,
    base: float = 1.0,
    max_delay: float = 30.0,
    jitter: bool = True,
) -> float:
    """Calculate exponential backoff with full jitter.

    Formula: min(max_delay, base * 2 ** attempt) with uniform random [0, delay].
    """
    ceiling = float(min(max_delay, base * (2**attempt)))
    if jitter:
        return float(random.uniform(0.0, ceiling))
    return float(ceiling)
