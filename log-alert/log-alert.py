#!/usr/bin/env python3
import argparse
import json
import jsonschema
import os
import re
import requests
import sys
import time
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional

# Base Classes
class LogFetcher(ABC):
    """Abstract base class for log fetchers."""

    @abstractmethod
    def fetch_logs(self, filters: Dict[str, Any], start_time: int, end_time: int) -> List[Dict[str, Any]]:
        pass

class Filter(ABC):
    """Abstract base class for filters."""

    @abstractmethod
    def filter(self, log: Dict[str, Any]) -> Dict[str, Any]:
        pass

class AlertManager(ABC):
    """Abstract base class for alert managers."""

    @abstractmethod
    def send_alert(self, title: str, message: str) -> None:
        pass

# Loki Log Fetcher
class LokiLogFetcher(LogFetcher):
    """Concrete implementation for fetching logs from Loki."""

    def __init__(self, config: Dict[str, Any]):
        self.url = config["url"]

    def fetch_logs(self, filters: Dict[str, Any], start_time: int, end_time: int) -> List[Dict[str, Any]]:
        """Fetch logs from Loki within the specified time range."""
        query = '{'
        for label in filters.get("labels", {}):
            if len(query) > 1:
                query += ','
            query += f'{label}="{filters["labels"][label]}"'
        query += '}'
        if "text" in filters:
            query += f' |= "{filters["text"]}"'
        print(f"Executing Loki query: {query}")
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
            print(f"Error fetching logs from Loki: {e}")
            return []

# Regexp Filter
class RegexpFilter(Filter):
    """Concrete implementation for Regexp filter."""

    def __init__(self, config: Dict[str, Any]):
        self.match = config["match"]

    def filter(self, log: Dict[str, Any]) -> Dict[str, Any]:
        match = re.search(self.match, log["log"])
        if match:
            # Only call groupdict() when there is a match
            groups = match.groupdict()
            print(f"Regex match for '{self.match}' in log: {groups}")
            if groups:
                log.setdefault("labels", {}).update(groups)
            return log
        # no match
        print(f"Regex did not match for pattern '{self.match}' in log: {log.get('log')}")
        return None

# Gotify Alert Manager
class GotifyAlertManager(AlertManager):
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
            print(f"Alert sent to Gotify: {title}")
        except requests.exceptions.RequestException as e:
            print(f"Error sending alert to Gotify: {e}")

# Alert Rule
class AlertRule:
    """Represents an alert rule with filters and alert template."""

    def __init__(self, log_fetchers: LogFetcher, alert_managers: AlertManager, config: Dict[str, Any]):
        self.name = config["name"]
        self.log_fetcher = log_fetchers[config["log-fetcher"]["name"]]
        self.fetcher_filters = config["log-fetcher"].get("filters", {})
        self.check_interval = config.get("check-interval", 60)
        self.filters = []
        for filter in config.get("filters", []):
            if filter["type"] == "regexp":
                self.filters.append(RegexpFilter(filter["config"]))
            else:
                raise ValueError(f"Unsupported filter type: {filter['type']}")
        self.alert_manager = alert_managers[config["alert-manager"]["name"]]
        self.alert_title = config["alert-manager"]["title"]
        self.alert_message = config["alert-manager"]["message"]
        self.last_run = time.time() - self.check_interval
        self.next_run = time.time()

    def run(self) -> None:
        print(f"Processing rule: {self.name}")
        logs = self.log_fetcher.fetch_logs(self.fetcher_filters, self.last_run, self.next_run)
        for log_entry in logs:
            print(f"Checking log: {log_entry['log']}")
            for filter in self.filters:
                log_entry = filter.filter(log_entry)
                if log_entry is None:
                    break
            if log_entry is None:
                continue
            message = self.alert_message.format_map(log_entry.get("labels", {}))
            print(f"Sending message: {message}, with params: {log_entry}")
            self.alert_manager.send_alert(self.alert_title, message)
        self.last_run = self.next_run
        self.next_run = time.time() + self.check_interval

# Main Application
class LogAlertApp:
    """Main application class to manage log fetching and alerting."""

    def __init__(self, config_path: str):
        self.config = self._load_config(config_path)
        print(f"Configuration loaded: {self.config}")
        self.log_fetchers = {}
        for fetcher in self.config["log-fetchers"]:
            self.log_fetchers[fetcher["name"]] = self._init_log_fetcher(fetcher)
        self.alert_managers = {}
        for manager in self.config["alert-managers"]:
            self.alert_managers[manager["name"]] = self._init_alert_manager(manager)
        self.alert_rules = [AlertRule(self.log_fetchers, self.alert_managers, rule) for rule in self.config["log-alerts"]]

    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """Load the configuration from a JSON file and validate it with JSON Schema."""
        try:
            with open(config_path, 'r') as config_file:
                # read JSON first
                config = json.load(config_file)
                # Perform schema validation if jsonschema is available
                self._validate_config_with_schema(config)
                # Update config to load env variable where required
                return self._update_config_from_env(config)
        except FileNotFoundError:
            print(f"Error: Configuration file '{config_path}' not found.")
            sys.exit(1)
        except json.JSONDecodeError:
            print(f"Error: Invalid JSON in configuration file '{config_path}'.")
            sys.exit(1)

    def _validate_config_with_schema(self, config: Dict[str, Any]) -> None:
        """Validate a loaded config dict against log-alert/config.schema.json if jsonschema is installed."""
        schema_path = os.path.join(os.path.dirname(__file__), 'config.schema.json')
        try:
            with open(schema_path, 'r') as sf:
                schema = json.load(sf)
            jsonschema.validate(instance=config, schema=schema)
        except FileNotFoundError:
            print(f"Warning: Schema file '{schema_path}' not found; skipping config validation.")
        except jsonschema.exceptions.ValidationError as e:
            print(f"Configuration validation error: {e.message}")
            print("Detailed error:", e)
            sys.exit(1)
        except Exception as e:
            print(f"Unexpected error while validating configuration: {e}")
            sys.exit(1)

    def _update_config_from_env(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Update config values from environment variables if specified."""
        for key, value in list(config.items()):
            if isinstance(value, dict):
                config[key] = self._update_config_from_env(value)
            elif isinstance(value, list):
                config[key] = [self._update_config_from_env(item) for item in value]
            elif isinstance(value, str) and key.endswith("-from-env"):
                new_key = key[:-9]  # Remove '-from-env'
                config[new_key] = value.format_map(os.environ)
                del config[key]
                return self._update_config_from_env(config) # re-evaluate in case of nested env vars
        return config

    def _init_log_fetcher(self, fetcher_config: Dict[str, Any]) -> LogFetcher:
        """Initialize the log fetcher based on config."""
        if fetcher_config["type"] == "loki":
            return LokiLogFetcher(fetcher_config["config"])
        else:
            raise ValueError(f"Unsupported log fetcher type: {fetcher_config['type']}")

    def _init_alert_manager(self, manager_config: Dict[str, Any]) -> AlertManager:
        """Initialize the alert manager based on config."""
        if manager_config["type"] == "gotify":
            return GotifyAlertManager(manager_config["config"])
        else:
            raise ValueError(f"Unsupported alert manager type: {manager_config['type']}")

    def run(self) -> None:
        """Fetch logs, check for matches, and send alerts."""
        while True:
            for rule in self.alert_rules:
                if time.time() >= rule.next_run:
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
