"""timestamp filter implementation."""

import logging
import requests
import datetime
from typing import Dict, Any, Optional
from zoneinfo import ZoneInfo

from .base import Filter

logger = logging.getLogger("log-alert")


class TimestampFilter(Filter):
    """Concrete implementation for Timestamp filter."""

    def __init__(self, config: Dict[str, Any]):
        self.source_field = config["source-field"]
        self.timezone = config.get("timezone", "UTC")
        self.timestamp_format = config.get("timestamp-format")

    def filter(self, log: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        timestamp_string = log.get("labels", {}).get(self.source_field)
        if timestamp_string is None:
            logger.warning(f"No timestamp found in log for field {self.source_field}")
            return None
        else:
            try:
                if self.timestamp_format:
                    logger.debug(f"Parsing timestamp {timestamp_string} with format {self.timestamp_format}")
                    parsed_time = datetime.datetime.strptime(timestamp_string, self.timestamp_format)
                    if parsed_time.year == 1900:
                        # If the year is not specified, assume the current year
                        parsed_time = parsed_time.replace(year=datetime.datetime.now().year)
                    timestamp_value = int(parsed_time.replace(tzinfo=ZoneInfo(self.timezone)).timestamp())
                else:
                    timestamp_value = int(timestamp_string)
            except Exception as e:
                logger.error(f"Error parsing timestamp {timestamp_value} with format {self.timestamp_format}: {e}")

            if timestamp_value >= log.get("fetcher-start-time", 0) and timestamp_value < log.get("fetcher-end-time", float('inf')):
                logger.debug(f"Log timestamp {timestamp_value} is within the fetcher time range.")
                return log
            else:
                logger.debug(f"Log timestamp {timestamp_value} is outside the fetcher time range.")
                return None
