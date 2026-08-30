"""Base classes for alert rules."""
from abc import ABC, abstractmethod
from typing import Dict, Any


class AlertRule(ABC):
    """Abstract base class for alert rules.

    Concrete implementations should implement the `run()` method and manage
    their own scheduling (`last_run`, `next_run`).
    """

    @abstractmethod
    def run(self) -> None:
        """Execute the alert rule logic."""
        raise NotImplementedError
