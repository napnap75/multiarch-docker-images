"""Parseable log fetcher implementation."""

import datetime
import logging
from time import time
import requests
from typing import Dict, Any, List

from .base import LogFetcher

logger = logging.getLogger("log-alert")


class ParseableLogFetcher(LogFetcher):
    """Concrete implementation for fetching logs from Parseable."""

    def __init__(self, config: Dict[str, Any]):
        self.url = config["url"]
        self.dataset = config["dataset"]
        self.user = config.get("user")
        self.password = config.get("password")
        self.last_fetched_time = int(time())  # Initialize with current time

    def fetch_logs(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fetch logs from Parseable without time range."""
        old_time = self.last_fetched_time
        self.last_fetched_time = int(time())
        return self.fetch_logs_time_range(filters, old_time, self.last_fetched_time)

    def fetch_logs_time_range(self, filters: Dict[str, Any], start_time: int, end_time: int) -> List[Dict[str, Any]]:
        """Fetch logs from Parseable within the specified time range."""
        query = 'SELECT * FROM \''
        query += f'{self.dataset}\' WHERE '
        labelNum = 0
        for label in filters.get("labels", {}):
            if labelNum > 0:
                query += ' AND '
            query += f'{label}=\'{filters["labels"][label]}\''
            labelNum += 1
        if "text" in filters:
            if labelNum > 0:
                query += ' AND '
            query += f'log LIKE \'%{filters["text"]}%\''
        payload = {
            "query": query,
            "startTime": datetime.datetime.fromtimestamp(start_time).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": datetime.datetime.fromtimestamp(end_time).strftime("%Y-%m-%dT%H:%M:%SZ")
        }
        logger.debug(f"Executing Parseable query: {payload}")
        try:
            response = requests.post(f"{self.url}/api/v1/query", json=payload, auth=(self.user, self.password), headers = { "Content-Type": "application/json" })
            response.raise_for_status()
            data = response.json()
            logs = []
            for item in data:
                timestamp = int(datetime.datetime.strptime(item.get("p_timestamp"), "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=datetime.timezone.utc).timestamp())
                if timestamp >= start_time and timestamp < end_time:
                    logs.append({
                        "timestamp": item.get("p_timestamp"),
                        "log": item.get("log"),
                        "labels": {k: v for k, v in item.items() if k not in ["p_timestamp", "log"]}
                    })
            return logs
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching logs from Parseable: {e}")
            return []
