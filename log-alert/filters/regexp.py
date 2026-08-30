"""Regexp filter implementation."""

import logging
import re
from typing import Dict, Any, Optional

from .base import Filter

logger = logging.getLogger("log-alert")


class RegexpFilter(Filter):
    """Concrete implementation for Regexp filter."""

    def __init__(self, config: Dict[str, Any]):
        self.match = config["match"]

    def filter(self, log: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        match = re.search(self.match, log["log"])
        if match:
            # Only call groupdict() when there is a match
            groups = match.groupdict()
            logger.debug(f"Regex match for '{self.match}' in log: {groups}")
            if groups:
                log.setdefault("labels", {}).update(groups)
            return log
        # no match
        logger.debug(f"Regex did not match for pattern '{self.match}' in log: {log.get('log')}")
        return None
