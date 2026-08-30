"""File log fetcher implementation."""

import logging
import requests
from typing import Dict, Any, List
from dateutil.parser import parse

from .base import LogFetcher

logger = logging.getLogger("log-alert")


class FileLogFetcher(LogFetcher):
    """Concrete implementation for fetching logs from a file."""

    type = "TAIL"

    def __init__(self, config: Dict[str, Any]):
        try:
            self.file = open(config["file"], "r")
        except FileNotFoundError:
            logger.error(f"Log file not found: {config['file']}")
            raise
        self.position = 0  # To keep track of the last read position

    def fetch_logs(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fetch logs from the file."""
        logs = []

        self.file.seek(self.position)  # Move to the last read position
        while True:
            self.position = self.file.tell()
            line = self.file.readline()
            if not line:
                break  # End of file reached, exit loop

            logger.debug(f"Read line: {line.strip()} at position {self.position}")

            parts = line.strip().split(maxsplit=1)
            timestamp_str, rest = parts[0], parts[1]

            # Parse the timestamp string into a datetime object
            dt = parse(timestamp_str)
            # Convert to seconds since epoch
            epoch_seconds = int(dt.timestamp())

            log_entry = {"timestamp": epoch_seconds, "log": rest}

            logs.append(log_entry)

        return logs

    def fetch_logs_time_range(self, filters: Dict[str, Any], start_time: int, end_time: int) -> List[Dict[str, Any]]:
        """Fetch logs from the file within the specified time range."""
        logs = []

        self.file.seek(0)
        while True:
            line = self.file.readline()
            if not line:
                break  # End of file reached, exit loop

            logger.debug(f"Read line: {line.strip()} at position {self.position}")

            parts = line.strip().split(maxsplit=1)
            timestamp_str, rest = parts[0], parts[1]

            dt = parse(timestamp_str)
            epoch_seconds = int(dt.timestamp())

            if epoch_seconds < start_time:
                continue
            if epoch_seconds > end_time:
                break

            log_entry = {"timestamp": epoch_seconds, "log": rest}

            logs.append(log_entry)

        return logs
    