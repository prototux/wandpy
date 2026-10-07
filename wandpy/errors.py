"""
Exceptions raised by WandPy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from wandpy.constants import Feature, WandType


class WandError(Exception):
    """Base class for all WandPy errors."""


class NotConnectedError(WandError, RuntimeError):
    """Raised when a command needs a connected wand."""

    def __init__(self, message: str = "Not connected to wand") -> None:
        super().__init__(message)


class UnsupportedFeatureError(WandError, NotImplementedError):
    """
    Raised when the connected wand does not have the requested feature.

    Check beforehand with `wand.supports(feature)` to avoid it.
    """

    def __init__(self, feature: "Feature", wand_type: Optional["WandType"] = None) -> None:
        self.feature = feature
        self.wand_type = wand_type
        target = f"the {wand_type.value} wand" if wand_type else "this wand"
        super().__init__(f"Feature '{feature.value}' is not supported by {target}")


class WandTimeoutError(WandError, TimeoutError):
    """Raised when the wand does not answer a request in time."""
