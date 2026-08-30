"""Abstract base class for filters."""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class Filter(ABC):
    """Abstract base class for filters."""

    @abstractmethod
    def filter(self, log: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Apply filter to a log entry.
        
        Args:
            log: Log entry dictionary
            
        Returns:
            Modified log entry, or None if the log should be filtered out
        """
        pass
