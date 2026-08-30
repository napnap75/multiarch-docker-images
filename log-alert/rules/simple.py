"""Simple AlertRule implementation copied from the previous monolithic implementation."""
import logging
import time
from typing import Dict, Any, Optional

from .base import AlertRule
from fetchers import LogFetcher
from filters import RegexpFilter, GeolocationFilter
from alerters import Alerter

logger = logging.getLogger("log-alert")


class SimpleAlertRule(AlertRule):
    """Represents an alert rule with filters and alert template."""

    def __init__(self, log_fetchers: Dict[str, LogFetcher], alerters: Dict[str, Alerter], config: Dict[str, Any]):
        self.log_fetcher = log_fetchers[config["log-fetcher"]["name"]]
        self.fetcher_filters = config["log-fetcher"].get("filters", {})
        self.check_interval = config.get("check-interval", 60)
        self.filters = []
        for filter in config.get("filters", []):
            if filter["type"] == "regexp":
                self.filters.append(RegexpFilter(filter["config"]))
            elif filter["type"] == "geolocation":
                self.filters.append(GeolocationFilter(filter["config"]))
            else:
                raise ValueError(f"Unsupported filter type: {filter['type']}")
        self.alerter = alerters[config["alerter"]["name"]]
        self.alert_title = config["alerter"]["title"]
        self.alert_message = config["alerter"]["message"]
        self.last_run = time.time() - self.check_interval
        self.next_run = time.time()

    def run(self) -> None:
        logs = self.log_fetcher.fetch_logs_time_range(self.fetcher_filters, start_time=self.last_run, end_time=self.next_run)
        for log_entry in logs:
            logger.debug(f"Checking log: {log_entry['log']}")
            for filter in self.filters:
                log_entry = filter.filter(log_entry)
                if log_entry is None:
                    break
            if log_entry is None:
                continue
            title = self.alert_title.format_map(log_entry.get("labels", {}))
            message = self.alert_message.format_map(log_entry.get("labels", {}))
            logger.info(f"Sending message: {message}, title: {title}, with params: {log_entry}")
            self.alerter.send_alert(title, message)
        self.last_run = self.next_run
        self.next_run = time.time() + self.check_interval
