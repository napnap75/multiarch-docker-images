"""Gotify alerter implementation."""

import logging
import requests
from typing import Dict, Any

from .base import Alerter

logger = logging.getLogger("log-alert")


class GotifyAlerter(Alerter):
    """Concrete implementation for Gotify alert manager."""

    def __init__(self, config: Dict[str, Any]):
        self.url = config["url"]
        self.token = config.get("token")

    def send_alert(self, title: str, message: str) -> None:
        """Send an alert to Gotify."""
        payload = {
            "title": title,
            "message": message,
            "priority": 5
        }
        try:
            response = requests.post(f"{self.url}?token={self.token}", json=payload)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error sending alert to Gotify: {e}")
