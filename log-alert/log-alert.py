import argparse
import json
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
            "start": str(start_time * 1000000000),  # Convert to nanoseconds
            "end": str(end_time * 1000000000),
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
                        "log": log
                    } | stream.get("stream", {}))
            return logs
        except requests.exceptions.RequestException as e:
            print(f"Error fetching logs from Loki: {e}")
            return []

# Gotify Alert Manager
class GotifyAlertManager(AlertManager):
    """Concrete implementation for Gotify alert manager."""

    def __init__(self, config: Dict[str, Any]):
        self.url = config["url"]
        self.token = config["token"]

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

    def __init__(self, config: Dict[str, Any]):
        self.name = config["name"]
        self.filters = config["filters"]
        self.alert_title = config["alert"]["title"]
        self.alert_message = config["alert"]["message"]

    def matches(self, log: str) -> Optional[Dict[str, str]]:
        """Check if the log matches the alert rule."""
        match = re.search(self.filters.get("match"), log)
        print(f"Regex match for '{self.filters.get('match')}' in log: {match.groupdict()}")
        if match:
            return match.groupdict()
        return None

# Main Application
class LogAlertApp:
    """Main application class to manage log fetching and alerting."""

    def __init__(self, config_path: str):
        self.config = self._load_config(config_path)
        self.log_fetcher = self._init_log_fetcher()
        self.alert_manager = self._init_alert_manager()
        self.alert_rules = [AlertRule(rule) for rule in self.config["log-alerts"]]
    
    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """Load the configuration from a JSON file."""
        try:
            with open(config_path, 'r') as config_file:
                return self._update_config_from_env(json.load(config_file))
        except FileNotFoundError:
            print(f"Error: Configuration file '{config_path}' not found.")
            sys.exit(1)
        except json.JSONDecodeError:
            print(f"Error: Invalid JSON in configuration file '{config_path}'.")
            sys.exit(1)

    def _update_config_from_env(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Update config values from environment variables if specified."""
        for key, value in config.items():
            if isinstance(value, dict):
                config[key] = self._update_config_from_env(value)
            elif isinstance(value, str) and key.endswith("-from-env"):
                config[key[0:-9]] = value.format_map(os.environ)
                del config[key]
                return self._update_config_from_env(config) # re-evaluate in case of nested env vars
        return config

    def _init_log_fetcher(self) -> LogFetcher:
        """Initialize the log fetcher based on config."""
        fetcher_config = self.config["log-fetcher"]
        if fetcher_config["type"] == "loki":
            return LokiLogFetcher(fetcher_config["config"])
        else:
            raise ValueError(f"Unsupported log fetcher type: {fetcher_config['type']}")

    def _init_alert_manager(self) -> AlertManager:
        """Initialize the alert manager based on config."""
        manager_config = self.config["alert-manager"]
        if manager_config["type"] == "gotify":
            return GotifyAlertManager(manager_config["config"])
        else:
            raise ValueError(f"Unsupported alert manager type: {manager_config['type']}")

    def run_once(self, start_time: int, end_time: int) -> None:
        for rule in self.alert_rules:
            print(f"Processing rule: {rule.name}")
            logs = self.log_fetcher.fetch_logs(rule.filters, start_time, end_time)
            for log_entry in logs:
                print(f"Checking log: {log_entry['log']}")
                match = rule.matches(log_entry["log"])
                log_entry.update(match)
                if match:
                    message = rule.alert_message.format_map(log_entry)
                    print(f"Sending message: {message}, with params: {log_entry}")
                    self.alert_manager.send_alert(rule.alert_title, message)

    def run(self, start_time: int, end_time: int) -> None:
        """Fetch logs, check for matches, and send alerts."""
        if start_time == 0 and end_time == 0:
            last_run = int(time.time())-self.config["check-interval"];
            while True:
                now = int(time.time())
                self.run_once(last_run, now)
                last_run = now
                time.sleep(self.config["check-interval"])
        else:
            if start_time == 0:
                start_time = int(time.time()) - self.config["check-interval"]
            if end_time == 0:
                end_time = int(time.time())
            self.run_once(start_time, end_time)
 
def main():
    parser = argparse.ArgumentParser(description="Log and Alert Management Tool")
    parser.add_argument("--config", required=True, help="Path to the configuration file")
    parser.add_argument("--start", type=int, default=0, help="Start time (Unix timestamp)")
    parser.add_argument("--end", type=int, default=0, help="End time (Unix timestamp)")
    args = parser.parse_args()

    app = LogAlertApp(args.config)
    app.run(args.start, args.end)

if __name__ == "__main__":
    main()
