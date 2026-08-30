"""Loki log fetcher implementation."""

import logging
from time import time
import requests
from typing import Dict, Any, List

from .base import LogFetcher

logger = logging.getLogger("log-alert")


class LokiLogFetcher(LogFetcher):
    """Concrete implementation for fetching logs from Loki."""

    def __init__(self, config: Dict[str, Any]):
        self.url = config["url"]
        self.last_fetched_time = int(time.time())

    def fetch_logs(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fetch logs from Loki without time range."""
        old_time = self.last_fetched_time
        self.last_fetched_time = int(time.time())
        return self.fetch_logs_time_range(filters, old_time, self.last_fetched_time)

    def fetch_logs_time_range(self, filters: Dict[str, Any], start_time: int, end_time: int) -> List[Dict[str, Any]]:
        """Fetch logs from Loki within the specified time range."""
        query = '{'
        for label in filters.get("labels", {}):
            if len(query) > 1:
                query += ','
            query += f'{label}="{filters["labels"][label]}"'
        query += '}'
        if "text" in filters:
            query += f' |= "{filters["text"]}"'
        logger.debug(f"Executing Loki query: {query}")
        payload = {
            "query": query,
            "limit": 1000,
            "start": str(int(start_time) * 1000000000),  # Convert to nanoseconds
            "end": str(int(end_time) * 1000000000),
            "direction": "forward"
        }
        try:
            response = requests.get(f"{self.url}/loki/api/v1/query_range", params=payload)
            response.raise_for_status()
            data = response.json()
            logs = []
            for stream in data.get("data", {}).get("result", []):
                for value in stream.get("values", []):
                    timestamp, log = value
                    logs.append({
                        "timestamp": timestamp,
                        "log": log,
                        "labels": stream.get("stream", {})
                    })
            return logs
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching logs from Loki: {e}")
            return []
