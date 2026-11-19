from __future__ import annotations

import logging
import sys

from .config import get_settings


def setup_logging(service: str) -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format=f"%(asctime)s %(levelname)-5s [{service}] %(name)s: %(message)s",
    )
    for noisy in ("pika", "httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
