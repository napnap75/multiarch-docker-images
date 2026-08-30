"""Rules package for alerting."""
from .base import AlertRule
from .simple import SimpleAlertRule

__all__ = ["AlertRule", "SimpleAlertRule"]
