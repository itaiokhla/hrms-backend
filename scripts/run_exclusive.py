"""Run one heavy command at a time on this machine.

    python scripts/run_exclusive.py -- <command> [arguments ...]

Database-backed test runs share one embedded PostgreSQL database, and type
checks, frontend builds and browser suites each need hundreds of megabytes of
memory. Every such command goes through this wrapper: it waits for a
machine-wide lock file, runs the command in the current directory, and
releases the lock (also when the command fails). scripts/pytest_serial.py uses
the same lock.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LOCK_PATH = Path(tempfile.gettempdir()) / "hrms_heavy_command.lock"
MAX_WAIT_SECONDS = int(os.environ.get("RUN_EXCLUSIVE_MAX_WAIT", "3600"))
POLL_SECONDS = 3


def _try_lock(handle) -> bool:
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_exclusive(command, cwd=None, shell=False) -> int:
    started = time.monotonic()
    with open(LOCK_PATH, "a+") as handle:
        announced = False
        while not _try_lock(handle):
            if not announced:
                print(f"run_exclusive: waiting for another heavy command to finish ({LOCK_PATH})", flush=True)
                announced = True
            if time.monotonic() - started > MAX_WAIT_SECONDS:
                print("run_exclusive: gave up waiting for the lock", file=sys.stderr)
                return 3
            time.sleep(POLL_SECONDS)
        try:
            if announced:
                print(f"run_exclusive: lock acquired after {time.monotonic() - started:.0f}s", flush=True)
            return subprocess.call(command, cwd=cwd, shell=shell)
        finally:
            _unlock(handle)


def main(argv) -> int:
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    if os.name == "nt":
        # Through cmd.exe so npx/npm (.cmd shims) resolve as they do in a terminal.
        return run_exclusive(subprocess.list2cmdline(argv), shell=True)
    return run_exclusive(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
