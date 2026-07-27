from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import UTC, datetime, timedelta

from app.jobs import BridgeService
from app.source_manager import SourceManager

logger = logging.getLogger(__name__)


class SourceScheduler:
    def __init__(self, source_manager: SourceManager, bridge: BridgeService) -> None:
        self.source_manager = source_manager
        self.bridge = bridge
        self._next_runs: dict[str, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._last_local_scan = 0.0

    async def run_forever(self) -> None:
        while True:
            now = time.monotonic()
            for source in self.source_manager.enabled_sources():
                due = self._next_runs.get(source.source_type, 0.0)
                if now < due or self._source_task_running(source.source_type):
                    continue
                interval = _jittered_interval(source.poll_seconds)
                self._next_runs[source.source_type] = now + interval
                next_poll = datetime.now(UTC) + timedelta(seconds=interval)
                self.source_manager.db.update_source_state(
                    source.source_type,
                    next_poll_at=next_poll.replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                )
                task = asyncio.create_task(
                    self._run_source(source.source_type),
                    name=f"source-sync:{source.source_type}",
                )
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

            if now - self._last_local_scan >= 15:
                self._last_local_scan = now
                await asyncio.to_thread(self.bridge.scan_once)
            await asyncio.sleep(1)

    async def _run_source(self, source_type: str) -> None:
        result = await asyncio.to_thread(
            self.source_manager.sync_source,
            source_type,
        )
        if result.downloaded:
            await asyncio.to_thread(self.bridge.scan_once)
        if not result.ok:
            logger.warning("%s", result.message)

    def _source_task_running(self, source_type: str) -> bool:
        name = f"source-sync:{source_type}"
        return any(not task.done() and task.get_name() == name for task in self._tasks)


def _jittered_interval(interval: int) -> float:
    return max(1.0, interval * random.uniform(0.9, 1.1))  # nosec B311
