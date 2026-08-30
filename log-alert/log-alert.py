#!/usr/bin/env python3
import argparse
import logging
import time
from typing import Dict, Any

from alerters.log import LogAlerter
from utils.logging import setup_logging, get_logger
from utils.config import load_config, validate_config_with_schema, update_config_from_env
from fetchers import LogFetcher, FileLogFetcher, LokiLogFetcher, ParseableLogFetcher
from filters import Filter, RegexpFilter, GeolocationFilter
from alerters import Alerter, GotifyAlerter

# Configure root logger
setup_logging()
logger = get_logger("log-alert")

from rules import SimpleAlertRule as AlertRule

# Main Application
class LogAlertApp:
    """Main application class to manage log fetching and alerting."""

    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        logger.debug(f"Configuration loaded: {self.config}")
        self.log_fetchers = {}
        for key, fetcher in self.config["log-fetchers"].items():
            self.log_fetchers[key] = self._init_log_fetcher(fetcher)
        self.alerters = {}
        for key, alerter in self.config["alerters"].items():
            self.alerters[key] = self._init_alerter(alerter)
        self.alert_rules = {}
        for key, rule in self.config["alerting-rules"].items():
            self.alert_rules[key] = AlertRule(self.log_fetchers, self.alerters, rule)

    def _init_log_fetcher(self, fetcher_config: Dict[str, Any]) -> LogFetcher:
        """Initialize the log fetcher based on config."""
        if fetcher_config["type"] == "file":
            return FileLogFetcher(fetcher_config["config"])
        elif fetcher_config["type"] == "loki":
            return LokiLogFetcher(fetcher_config["config"])
        elif fetcher_config["type"] == "parseable":
            return ParseableLogFetcher(fetcher_config["config"])
        else:
            raise ValueError(f"Unsupported log fetcher type: {fetcher_config['type']}")

    def _init_alerter(self, manager_config: Dict[str, Any]) -> Alerter:
        """Initialize the alert manager based on config."""
        if manager_config["type"] == "log":
            return LogAlerter(manager_config["config"])
        elif manager_config["type"] == "gotify":
            return GotifyAlerter(manager_config["config"])
        else:
            raise ValueError(f"Unsupported alert manager type: {manager_config['type']}")

    def run(self) -> None:
        """Fetch logs, check for matches, and send alerts."""
        while True:
            for name, rule in self.alert_rules.items():
                if time.time() >= rule.next_run:
                    logger.debug(f"Processing rule: {name}")
                    rule.run()
            time.sleep(5)

def main():
    parser = argparse.ArgumentParser(description="Log and Alert Management Tool")
    parser.add_argument("--config", required=True, help="Path to the configuration file")
    args = parser.parse_args()

    app = LogAlertApp(args.config)
    app.run()

if __name__ == "__main__":
    main()
