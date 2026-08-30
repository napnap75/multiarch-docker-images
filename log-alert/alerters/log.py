"""Standard output alerter implementation."""

import logging
import requests
from typing import Dict, Any

from .base import Alerter

logger = logging.getLogger("log-alert")


class LogAlerter(Alerter):
    """Concrete implementation for log alert manager."""

    def __init__(self, config: Dict[str, Any]):
        pass

    def send_alert(self, title: str, message: str) -> None:
        """Send an alert to the log."""
        logger.info(f"[ALERT] {title} / {message}")
