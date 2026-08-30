"""Geolocation filter implementation."""

import logging
import requests
from typing import Dict, Any, Optional

from .base import Filter

logger = logging.getLogger("log-alert")


class GeolocationFilter(Filter):
    """Concrete implementation for Geolocation filter."""

    def __init__(self, config: Dict[str, Any]):
        self.source_field = config["source-field"]

    def filter(self, log: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        ip_address = log.get("labels", {}).get(self.source_field)
        if not ip_address:
            logger.warning("No IP address found in log labels for geolocation")
        else:
            try:
                response = requests.get(f"http://ip-api.com/json/{ip_address}").json()
                if response["status"] == "success":
                    logger.debug(f"Found info {response} for IP {ip_address}")
                    del response["status"]
                    del response["query"]
                    log.setdefault("labels", {}).update(response)
                else:
                    logger.warning("No info found for IP {ip_address}")
            except requests.exceptions.RequestException as e:
                logger.error(f"Error fetching geolocation for IP {ip_address}: {e}")
        return log
