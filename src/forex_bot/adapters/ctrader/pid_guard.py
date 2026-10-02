"""PID file guard — single-instance enforcement via atomic file locking.

Uses ``fcntl.flock(LOCK_EX | LOCK_NB)`` for atomic acquisition (no TOCTOU race).
Falls back to ``/proc/{pid}`` check for stale-PID detection when the lock is
held by a dead process.

Usage::

    from adapters.ctrader.pid_guard import acquire_pid_lock

    with acquire_pid_lock("data/forward_test.pid") as guard:
        # ... only one process can be here ...
        guard.write_pid()
"""

import fcntl
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger("ayumi.pid_guard")


class PIDLockGuard:
    """Manages a PID file with an ``fcntl.flock`` advisory lock.

    The lock is held for the lifetime of this object (used as a context manager
    via ``acquire_pid_lock``).  Callers should invoke ``write_pid()`` after
    entering the context to record their PID.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path).resolve()
        self._fd: int | None = None
        self._locked: bool = False

    # --- public ---

    def write_pid(self):
        """Write the current PID to the lock file (call after entering context)."""
        if self._fd is None:
            raise RuntimeError("Lock not held — cannot write PID")
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.ftruncate(self._fd, 0)
        os.write(self._fd, f"{os.getpid()}\n".encode())
        logger.info("PID %d written to %s", os.getpid(), self._path)

    # --- context manager internals ---

    def _acquire(self):
        """Acquire the PID lock atomically.

        1. Ensure the PID file (and parent dirs) exist.
        2. Open the file and try ``fcntl.flock(LOCK_EX | LOCK_NB)``.
        3. If the lock is held, read the PID and check ``/proc/{pid}``.
           - If the process is alive → refuse to start.
           - If dead → force-unlock the stale lock and retry.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)

        attempt = 0
        while True:
            attempt += 1
            fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o666)

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                # Lock is held — read PID and check if process is alive
                try:
                    data = os.read(fd, 64).decode().strip()
                    existing_pid = int(data)
                except (ValueError, OSError):
                    existing_pid = None

                os.close(fd)

                if existing_pid is not None and _is_pid_alive(existing_pid):
                    logger.error(
                        "Another forward test process is running (PID %d). "
                        "Refusing to start. Kill it first or remove %s.",
                        existing_pid,
                        self._path,
                    )
                    sys.exit(1)

                # Stale lock — forcibly clear it
                logger.warning(
                    "Stale PID %d found in %s — clearing",
                    existing_pid,
                    self._path,
                )
                _force_clear_stale(self._path)
                if attempt >= 3:
                    logger.error("Failed to acquire PID lock after %d attempts", attempt)
                    sys.exit(1)
                continue  # retry

            # Lock acquired successfully
            self._fd = fd
            self._locked = True
            return

    def _release(self):
        """Release the lock and close the file descriptor."""
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            self._locked = False

            # Clean up PID file on exit
            try:
                self._path.unlink(missing_ok=True)
                logger.info("PID file %s removed", self._path)
            except OSError:
                pass

    def __enter__(self):
        self._acquire()
        return self

    def __exit__(self, *exc):
        self._release()
        return False


@contextmanager
def acquire_pid_lock(path: str | Path) -> Generator[PIDLockGuard, None, None]:
    """Context manager: acquire a PID-file lock or exit.

    Usage::

        with acquire_pid_lock("data/forward_test.pid") as guard:
            guard.write_pid()
            # ... main work ...

    If another live process holds the lock, the process exits with code 1
    and a clear log message.
    """
    guard = PIDLockGuard(path)
    with guard:
        yield guard


def _is_pid_alive(pid: int) -> bool:
    """Check whether a process is alive via ``/proc/{pid}``."""
    try:
        os.kill(pid, 0)  # signal 0 = existence check
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — treat as alive
        return True
    except OSError:
        return False


def _force_clear_stale(path: Path):
    """Force-clear a stale PID file by opening it exclusively and truncating."""
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)  # blocking — wait for stale holder
            os.ftruncate(fd, 0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    except OSError as exc:
        logger.warning("Failed to force-clear stale PID file %s: %s", path, exc)
