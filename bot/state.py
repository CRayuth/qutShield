"""Track each user's active download and its temporary resources."""

import asyncio
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Job:
    user_id: int
    chat_id: int
    status_message_id: int
    allow_selection: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    stopped: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task | None = None
    temporary_directory: Path | None = None
    error_text: str | None = None
    cancelled_status_ids: list[int] = field(default_factory=list)
    status_text: str = ""
    status_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    selection_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    selection: "SelectionSession | None" = None


@dataclass
class SelectionSession:
    message_id: int
    count: int
    result: asyncio.Future[list[int]]
    selected: set[int] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class QueuedJob:
    job: Job
    message: Any
    inputs: list[str]


class JobManager:
    def __init__(self):
        self.jobs: dict[tuple[int, int], Job] = {}
        self.pending: dict[tuple[int, int], deque[QueuedJob]] = {}
        try:
            workers = int(os.getenv("MEDIA_WORKERS", "4"))
        except ValueError:
            workers = 4
        self.media_workers = max(1, min(workers, 8))
        self.slots = asyncio.Semaphore(self.media_workers)
        self.resolve_slots = asyncio.Semaphore(4)
        self.last_started: dict[tuple[int, int], float] = {}

    def rate_limited(self, key: tuple[int, int]) -> bool:
        return time.monotonic() - self.last_started.get(key, float("-inf")) < 2

    def register(self, job: Job) -> bool:
        key = (job.chat_id, job.user_id)
        if key in self.jobs or len(self.jobs) >= 20:
            return False
        self.jobs[key] = job
        self.last_started[key] = time.monotonic()
        if len(self.last_started) > 1000:
            cutoff = time.monotonic() - 60
            self.last_started = {item: started for item, started in self.last_started.items() if started > cutoff}
        return True

    def submit(self, item: QueuedJob) -> str:
        key = (item.job.chat_id, item.job.user_id)
        if key in self.jobs:
            queue = self.pending.setdefault(key, deque())
            if len(queue) >= 5 or sum(map(len, self.pending.values())) >= 50:
                return "full"
            queue.append(item)
            return "queued"
        return "started" if self.register(item.job) else "full"

    def stop(self, key: tuple[int, int]) -> Job | None:
        job = self.jobs.get(key)
        if job:
            job.stopped.set()
            if job.selection and not job.selection.result.done():
                job.selection.result.cancel()
            job.cancelled_status_ids.extend(item.job.status_message_id for item in self.pending.pop(key, ()))
        return job

    def finish(self, job: Job) -> QueuedJob | None:
        key = (job.chat_id, job.user_id)
        if self.jobs.get(key) is job:
            self.jobs.pop(key, None)
            queue = self.pending.get(key)
            if queue:
                next_item = queue.popleft()
                if not queue:
                    self.pending.pop(key, None)
                self.jobs[key] = next_item.job
                self.last_started[key] = time.monotonic()
                return next_item
        return None
