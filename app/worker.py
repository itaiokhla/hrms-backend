"""Background job runner (decision I-09; 3.9 §6).

    python -m app.worker [--poll 2.0] [--batch 10] [--once]

Loops: re-queue stale jobs (reaper, every minute), claim due jobs with SKIP LOCKED, run each in a
session bound to its tenant. Several workers may run side by side. With JOBS_MODE=thread the API process
runs the same loop in an embedded daemon thread (start_embedded / stop_embedded, wired in app.main).
"""

import argparse
import logging
import signal
import threading
import time
from typing import Optional

from app.config import settings
from app.payroll.services import jobs

logger = logging.getLogger("hrms.worker")

REAP_EVERY_SECONDS = 60


class Worker:
    def __init__(self, kind: str = "worker", poll_seconds: float = 2.0, batch: int = jobs.CLAIM_BATCH) -> None:
        self.worker_id = jobs.default_worker_id(kind)
        self.poll_seconds = poll_seconds
        self.batch = batch
        self._last_reap = 0.0

    def tick(self) -> int:
        """One pass: reap when due, then claim and run one batch. Returns how many jobs ran."""
        now = time.monotonic()
        if now - self._last_reap >= REAP_EVERY_SECONDS:
            self._last_reap = now
            jobs.reap()
        claimed = jobs.claim(self.worker_id, limit=self.batch)
        for job in claimed:
            jobs.run_job(job, self.worker_id)
        return len(claimed)

    def run(self, stop: threading.Event) -> None:
        logger.info("Job worker %s started", self.worker_id)
        while not stop.is_set():
            try:
                ran = self.tick()
            except Exception:  # database unreachable or similar: wait and try again
                logger.exception("Job worker pass failed")
                ran = 0
            if ran == 0:
                stop.wait(self.poll_seconds)
        logger.info("Job worker %s stopped", self.worker_id)


def validate_mode() -> str:
    mode = settings.jobs_mode
    if mode not in jobs.MODES:
        raise RuntimeError(f"JOBS_MODE must be one of {', '.join(jobs.MODES)}")
    if mode == "inline" and settings.environment in ("staging", "production"):
        raise RuntimeError("JOBS_MODE=inline is for tests only")
    return mode


_embedded: Optional[threading.Thread] = None
_embedded_stop = threading.Event()


def start_embedded(poll_seconds: float = 1.0) -> threading.Thread:
    """Start the in-process worker thread (JOBS_MODE=thread); a second call returns the running thread."""
    global _embedded
    if _embedded is not None and _embedded.is_alive():
        return _embedded
    jobs.load_handlers()
    _embedded_stop.clear()
    worker = Worker(kind="thread", poll_seconds=poll_seconds)
    _embedded = threading.Thread(target=worker.run, args=(_embedded_stop,), name="hrms-job-worker", daemon=True)
    _embedded.start()
    return _embedded


def stop_embedded(timeout: float = 15.0) -> None:
    """Ask the embedded worker to stop after its current job and wait for it."""
    global _embedded
    _embedded_stop.set()
    if _embedded is not None:
        _embedded.join(timeout)
        if _embedded.is_alive():
            logger.warning("The job worker thread is still finishing a job; it stops when that job ends")
    _embedded = None


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.worker", description="Run payroll background jobs.")
    parser.add_argument("--poll", type=float, default=2.0, help="seconds to wait when no job is due")
    parser.add_argument("--batch", type=int, default=jobs.CLAIM_BATCH, help="jobs claimed per pass")
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=settings.log_level)
    jobs.install_log_redaction()
    try:
        validate_mode()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    from app.payroll.services import keys

    keys.check_master_key()
    jobs.load_handlers()

    worker = Worker(poll_seconds=args.poll, batch=args.batch)
    if args.once:
        worker.tick()
        return
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
    worker.run(stop)


if __name__ == "__main__":
    main()
