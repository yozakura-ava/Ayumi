"""Resource limits for CPU and memory throttling of test/backtest workloads.

Prevents runaway Python processes (backtests, walk-forward, Optuna, unit tests)
from consuming more than a configured share of server resources.

Usage (context manager):

    with cpu_limited(percent=20):
        run_backtest(...)

    with memory_capped(mb=2048):
        run_walk_forward(...)

Usage (decorator):

    @run_limited(cpu_percent=20, memory_mb=2048)
    def run_optuna(...):
        ...

CLI flags for scripts:

    python scripts/run_wf.py --max-cpu 20 --max-memory-mb 2048
"""

import logging
import os
import resource
import threading
from contextlib import contextmanager
from functools import wraps
from typing import Optional

logger = logging.getLogger("ayumi.resource_limits")

_NCPUS = os.cpu_count() or 1
_CGROUP_ROOT = "/sys/fs/cgroup"
_cgroup_lock = threading.Lock()


def _set_cpu_affinity(percent: int = 20) -> None:
    """Pin process to a subset of cores to enforce CPU limit (advisory/fallback).

    20% on an 8-core machine = ~1.6 cores. We pin to floor(cores * percent/100)
    cores, minimum 1, and set nice to 19.

    This is the fallback when cgroup v2 is unavailable. It provides advisory
    limits only — actual CPU usage may exceed the target.
    """
    try:
        import psutil

        proc = psutil.Process(os.getpid())
        allowed = max(1, int(_NCPUS * percent / 100))
        all_cores = list(range(_NCPUS))
        proc.cpu_affinity(all_cores[:allowed])
        logger.debug("CPU affinity set to %d/%d cores (%d%%)", allowed, _NCPUS, percent)
    except ImportError:
        logger.warning("psutil not available — cannot set CPU affinity")
    except OSError as e:
        logger.warning("CPU affinity failed: %s", e)

    try:
        os.nice(19)
        logger.debug("Nice set to 19")
    except OSError:
        pass


def _cgroup_v2_available() -> bool:
    """Check whether cgroup v2 with cpu controller is usable on this host.

    On cgroup v2, the root cgroup (``/sys/fs/cgroup``) does not have a
    ``cpu.max`` file — it cannot be limited itself.  Child cgroups gain
    ``cpu.max`` once the ``cpu`` controller is enabled via the parent's
    ``cgroup.subtree_control``.  We therefore check:

    1. The mount is cgroup v2.
    2. ``cpu`` is listed in ``cgroup.controllers`` (available to be enabled).
    3. ``cpu`` is enabled in the root ``cgroup.subtree_control`` (so that
       direct children of root get the controller).
    """
    if not os.path.isdir(_CGROUP_ROOT):
        return False
    # Must be cgroup v2 — check the filesystem type via mountinfo
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == _CGROUP_ROOT and parts[2] == "cgroup2":
                    break
            else:
                return False
    except OSError:
        return False
    # cpu controller must be available
    try:
        with open(f"{_CGROUP_ROOT}/cgroup.controllers") as f:
            controllers = f.read().split()
    except OSError:
        return False
    if "cpu" not in controllers:
        return False
    # cpu must be enabled in subtree_control so children get it
    try:
        with open(f"{_CGROUP_ROOT}/cgroup.subtree_control") as f:
            subtree = f.read().split()
    except OSError:
        return False
    return "cpu" in subtree


def _get_current_cgroup() -> str:
    """Return the cgroup path the current process belongs to."""
    try:
        with open(f"/proc/{os.getpid()}/cgroup") as f:
            line = f.readline().strip()
            # cgroup v2 format: "0::/relative/path"
            if "::" in line:
                rel = line.split("::", 1)[1]
                return f"{_CGROUP_ROOT}{rel}"
    except OSError:
        pass
    return _CGROUP_ROOT


def _set_cgroup_cpu_limit(percent: int = 20) -> Optional[str]:
    """Create a cgroup v2 CPU limit for the current process.

    Creates a transient cgroup, writes cpu.max to enforce a hard CPU cap,
    and migrates the current process into it.

    Args:
        percent: Maximum CPU percentage relative to total server capacity.\n                 20 = 20%% of all cores combined.

    Returns:
        The cgroup path on success, or None if cgroup v2 is unavailable
        or creation fails (caller should fall back to advisory limits).
    """  # noqa: E501
    if not _cgroup_v2_available():
        return None

    pid = os.getpid()
    cgroup_path = f"{_CGROUP_ROOT}/ayumi_cpu_{pid}"

    with _cgroup_lock:
        try:
            os.mkdir(cgroup_path)
        except PermissionError:
            logger.warning("cgroup v2: cannot create cgroup (permission denied) — falling back to advisory limits")
            return None
        except FileExistsError:
            # Stale cgroup from a previous run — try to clean and recreate
            try:
                os.rmdir(cgroup_path)
                os.mkdir(cgroup_path)
            except OSError:
                logger.warning(
                    "cgroup v2: stale cgroup at %s cannot be removed — falling back to advisory limits",
                    cgroup_path,
                )
                return None

        try:
            # Set CPU quota via cpu.max.
            # Format: "quota period" (microseconds).
            # Total CPU capacity = ncpus * 100%%.
            # quota = percent/100 * ncpus * period
            period = 100_000  # 100 ms standard period
            quota = max(1_000, int(percent * _NCPUS * period / 100))
            with open(f"{cgroup_path}/cpu.max", "w") as f:
                f.write(f"{quota} {period}")

            # Migrate the current process into the cgroup
            with open(f"{cgroup_path}/cgroup.procs", "w") as f:
                f.write(str(pid))

            logger.debug(
                "cgroup v2 CPU limit set: %d%% via %s (quota=%d, period=%d)",
                percent,
                cgroup_path,
                quota,
                period,
            )
            return cgroup_path
        except (OSError, PermissionError) as e:
            logger.warning("cgroup v2 CPU limit failed: %s — falling back to advisory limits", e)
            _cleanup_cgroup(cgroup_path)
            return None


def _cleanup_cgroup(cgroup_path: Optional[str]) -> None:
    """Move the current process back to root cgroup and remove the cgroup.

    Args:
        cgroup_path: Path returned by _set_cgroup_cpu_limit, or None.
    """
    if not cgroup_path:
        return

    pid = os.getpid()

    # Move process back to root cgroup
    try:
        with open(f"{_CGROUP_ROOT}/cgroup.procs", "w") as f:
            f.write(str(pid))
    except OSError:
        pass  # Process may already be in root cgroup

    # Remove the transient cgroup
    try:
        os.rmdir(cgroup_path)
        logger.debug("cgroup v2 cleaned up: %s", cgroup_path)
    except OSError as e:
        logger.warning("cgroup v2 cleanup failed for %s: %s", cgroup_path, e)


def _set_memory_limit(mb: int = 2048) -> None:
    """Set hard memory limit via RLIMIT_AS."""
    if mb <= 0:
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        new_limit = mb * 1024 * 1024
        # Only lower the limit; never raise above current hard
        if hard == resource.RLIM_INFINITY or new_limit < hard:
            resource.setrlimit(resource.RLIMIT_AS, (new_limit, new_limit))
            logger.debug("Memory limit set to %d MB", mb)
    except (ValueError, OSError) as e:
        logger.warning("Memory limit failed: %s", e)


@contextmanager
def cpu_limited(percent: int = 20):
    """Context manager that limits CPU usage for the duration of the block.

    Uses cgroup v2 ``cpu.max`` for hard enforcement when available, falling
    back to ``psutil.cpu_affinity`` + ``os.nice(19)`` for advisory limits.

    Args:
        percent: Maximum CPU percentage (relative to total server capacity).
                 20 = 20%% of total cores.

    Note:
        Nested ``cpu_limited()`` calls are not fully supported under cgroup
        mode — the inner cleanup restores the process to the root cgroup,
        not the outer cgroup.  Use a single ``cpu_limited()`` wrapper when
        possible.
    """
    cgroup_path = _set_cgroup_cpu_limit(percent)
    if cgroup_path is None:
        # Advisory fallback
        _set_cpu_affinity(percent)
    try:
        yield
    finally:
        _cleanup_cgroup(cgroup_path)


@contextmanager
def memory_capped(mb: int = 2048):
    """Context manager that sets a hard memory limit for the duration.

    Args:
        mb: Memory limit in megabytes. MemoryError raised if exceeded.
    """
    old_soft, old_hard = resource.getrlimit(resource.RLIMIT_AS)
    _set_memory_limit(mb)
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_AS, (old_soft, old_hard))


def run_limited(cpu_percent: int = 20, memory_mb: int = 2048):
    """Decorator that applies CPU and memory limits to a function.

    Useful for wrapping backtest runners, walk-forward scripts, Optuna optimization.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with cpu_limited(percent=cpu_percent):
                with memory_capped(mb=memory_mb):
                    return func(*args, **kwargs)

        return wrapper

    return decorator


def configure_pytest_defaults(max_memory_mb: int = 2048) -> None:
    """Configure pytest defaults for memory-limited test runs.

    Call from conftest.py at module scope.
    """
    _set_memory_limit(max_memory_mb)


# ── CLI helper for argparse-based scripts ────────────────────────────────


def add_resource_args(parser) -> None:
    """Add --max-cpu and --max-memory-mb flags to an argparse parser.

    Usage in scripts:

        import argparse
        from common.resource_limits import add_resource_args

        parser = argparse.ArgumentParser()
        add_resource_args(parser)
        args = parser.parse_args()

        with cpu_limited(args.max_cpu):
            with memory_capped(args.max_memory_mb):
                main()
    """
    parser.add_argument(
        "--max-cpu",
        type=int,
        default=20,
        help="Max CPU percentage (default: 20%% of server)",
    )
    parser.add_argument(
        "--max-memory-mb",
        type=int,
        default=2048,
        help="Max memory in MB per process (default: 2048)",
    )
