from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import RLock
from typing import Callable
import traceback

from app.models import Job
from app.services.cache import TTLCache


ProgressCallback = Callable[[int, str], None]
ValidatorCallable = Callable[[ProgressCallback], dict]


class ValidatorJobManager:
    def __init__(self, ttl_seconds: int, max_workers: int = 2) -> None:
        self.jobs: TTLCache[Job] = TTLCache(ttl_seconds)
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self._lock = RLock()

    def start(self, validator: ValidatorCallable) -> Job:
        job = Job(tracker_type="excel_validator")
        self.jobs.set(job.job_id, job)
        self.executor.submit(self._run_job, job.job_id, validator)
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def _run_job(self, job_id: str, validator: ValidatorCallable) -> None:
        self._update(job_id, status="running", progress_percent=1, message="Starting workbook validation")

        def progress(percent: int, message: str) -> None:
            self._update(job_id, progress_percent=percent, message=message)

        try:
            result = validator(progress)
            self._update(
                job_id,
                status="completed",
                progress_percent=100,
                message="Workbook validation complete",
                result=result,
            )
        except Exception as exc:
            traceback.print_exc()
            self._update(
                job_id,
                status="failed",
                progress_percent=100,
                message="Workbook validation failed",
                error_message=str(exc) or exc.__class__.__name__,
            )

    def _update(self, job_id: str, **changes) -> None:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = datetime.now(timezone.utc)
            self.jobs.set(job_id, job)
