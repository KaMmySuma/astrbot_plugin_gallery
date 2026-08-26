from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class GalleryScheduler:
    """Own exactly one cancellable periodic update task."""

    def __init__(
        self, interval_minutes: int, callback: Callable[[], Awaitable[None]]
    ) -> None:
        self.interval_seconds = max(1, interval_minutes) * 60
        self.callback = callback
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Cancel an old task and start a fresh scheduler task."""
        self.stop()
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        """Cancel the current scheduler task if present."""
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def wait_stopped(self) -> None:
        """Wait for a cancelled task to finish without propagating cancellation."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            try:
                await self.callback()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.interval_seconds)
