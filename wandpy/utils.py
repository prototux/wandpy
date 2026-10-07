"""
Utility functions for WandPy.

Small helpers used internally by the library.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, Optional, Set

# Module-level logger
logger = logging.getLogger("wandpy")

# Keep strong references to tasks spawned for async callbacks, otherwise the
# event loop may garbage collect them mid-flight.
_background_tasks: Set[asyncio.Task] = set()


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


def spawn(coro: Any) -> asyncio.Task:
    """Run a coroutine in the background, keeping a reference until it finishes."""
    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(_log_task_exception)
    return task


def _log_task_exception(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("Error in background task: %r", task.exception())


def invoke(callback: Optional[Callable[..., Any]], *args: Any, name: str = "callback") -> None:
    """
    Call a user callback, sync or async, without letting it break the library.

    Coroutine functions are scheduled as tasks so callbacks can simply be
    `async def` and await wand commands.
    """
    if callback is None:
        return
    try:
        result = callback(*args)
        if inspect.isawaitable(result):
            spawn(result)
    except Exception as e:
        logger.error(f"Error in {name}: {e}")
