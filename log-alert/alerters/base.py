"""Abstract base class for alerters."""

from abc import ABC, abstractmethod


class Alerter(ABC):
    """Abstract base class for alerting implementations."""

    @abstractmethod
    def send_alert(self, title: str, message: str) -> None:
        """Send an alert.

        Args:
            title: Alert title
            message: Alert message
        """
        pass
