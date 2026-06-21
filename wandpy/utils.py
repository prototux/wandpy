"""
Utility functions for WandPy.

Small helpers used internally by the library.
"""

import logging

# Module-level logger
logger = logging.getLogger("wandpy")

def setup_logging(level: int = logging.INFO) -> None:
    """
    Configure basic logging for the wandpy library.

    Args:
        level: Logging level (default: INFO). Use logging.DEBUG for
               verbose BLE communication details.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
