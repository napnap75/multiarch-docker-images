"""Abstract base class for log fetchers."""

from abc import ABC, abstractmethod
from typing import Dict, Any, List


class LogFetcher(ABC):
    """Abstract base class for log fetchers."""

    """Fetch logs."""
    @abstractmethod
    def fetch_logs(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fetch logs within the specified time range.
        
        Args:
            filters: Dictionary of filters to apply
            
        Returns:
            List of log entries
        """
        pass

    """ Fetch logs with time range."""
    @abstractmethod
    def fetch_logs_time_range(self, filters: Dict[str, Any], start_time: int, end_time: int) -> List[Dict[str, Any]]:
        """Fetch logs within the specified time range.
        
        Args:
            filters: Dictionary of filters to apply
            start_time: Start time in seconds since epoch
            end_time: End time in seconds since epoch
            
        Returns:
            List of log entries
        """
        pass
