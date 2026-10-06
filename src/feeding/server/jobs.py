"""A minimal background job queue for the UI.

One worker thread runs jobs sequentially: SLEAP inference saturates the GPU/CPU,
so running videos in parallel would not be faster. Each job belongs to a
project and mirrors its log to that project's ``paths.logs/{job_id}.log``.
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Job:
    id: str
    kind: str
    label: str
    fn: Callable[["Job"], object] = field(repr=False)
    video: str | None = None
    project: str | None = None
    project_name: str | None = None
    status: str = "queued"  # queued | running | done | failed | cancelled
    progress: dict = field(default_factory=dict)
    result: object = None
    error: str | None = None
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    cancel_requested: bool = False
    log_lines: deque = field(default_factory=lambda: deque(maxlen=400))
    log_path: Path | None = None

    def log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_lines.append(f"[{stamp}] {line}")
        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(f"[{stamp}] {line}\n")

    def to_dict(self, with_log: bool = False) -> dict:
        d = {k: getattr(self, k) for k in ("id", "kind", "label", "video", "project", "project_name", "status",
                                           "progress", "error", "created", "started", "finished")}
        d["result"] = str(self.result) if self.result is not None else None
        if with_log:
            d["log"] = list(self.log_lines)
        return d


class JobQueue:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._q: queue.Queue[Job] = queue.Queue()
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True, name="feeding-jobs").start()

    def submit(self, kind: str, label: str, fn: Callable[[Job], object], video: str | None = None,
               project: str | None = None, project_name: str | None = None, log_dir: Path | None = None) -> Job:
        with self._lock:
            jid = f"{time.strftime('%Y%m%d-%H%M%S')}-{next(self._ids):03d}"
            if log_dir is not None:
                log_dir.mkdir(parents=True, exist_ok=True)
            job = Job(id=jid, kind=kind, label=label, fn=fn, video=video, project=project, project_name=project_name,
                      log_path=log_dir / f"{jid}.log" if log_dir else None)
            self._jobs[jid] = job
        self._q.put(job)
        return job

    def active_for(self, video: str, kind: str, project: str | None = None) -> Job | None:
        return next((j for j in self._jobs.values()
                     if j.video == video and j.kind == kind and j.project == project
                     and j.status in ("queued", "running")), None)

    def get(self, jid: str) -> Job | None:
        return self._jobs.get(jid)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def cancel(self, jid: str) -> None:
        job = self._jobs.get(jid)
        if job and job.status in ("queued", "running"):
            job.cancel_requested = True
            if job.status == "queued":
                job.status = "cancelled"
                job.finished = time.time()

    def clear_finished(self) -> None:
        with self._lock:
            for jid in [j.id for j in self._jobs.values() if j.status in ("done", "failed", "cancelled")]:
                del self._jobs[jid]

    def _worker(self) -> None:
        while True:
            job = self._q.get()
            if job.status == "cancelled":
                continue
            job.status, job.started = "running", time.time()
            job.log(f"started: {job.label}")
            try:
                job.result = job.fn(job)
                job.status = "cancelled" if job.cancel_requested else "done"
            except InterruptedError:
                job.status = "cancelled"
            except Exception as exc:
                job.status, job.error = "failed", str(exc)
                job.log(traceback.format_exc())
            job.finished = time.time()
            job.log(f"{job.status} after {job.finished - job.started:.0f}s")
