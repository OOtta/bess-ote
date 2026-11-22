"""Worker pool for independent DP solves (``--jobs``).

Processes are used because each DP step is a small numpy operation (≈36 000 elements) that
holds the GIL most of the time – threads scale to only ~2× while processes scale with the
cores (measured). Where a process pool cannot be created (e.g. Termux/Android without a
working ``sem_open``) or breaks, the caller falls back to computing serially; results are
identical either way because every solve is a pure, deterministic function of its inputs.
"""

from __future__ import annotations

import os
from concurrent.futures import Executor, ProcessPoolExecutor

#: below this many quarter-hours of DP work a pool costs more than it saves
MIN_UNITS_PER_WORKER = 1500


def auto_jobs() -> int:
    return max(1, os.cpu_count() or 1)


def resolve_jobs(jobs: int | None) -> int:
    """``None`` / 0 = auto (``os.cpu_count()``), otherwise at least 1."""
    return auto_jobs() if not jobs else max(1, int(jobs))


def make_process_pool(workers: int) -> Executor | None:
    """Process pool with the platform's default start method, or None if unavailable."""
    try:
        return ProcessPoolExecutor(max_workers=workers)
    except (ImportError, OSError, NotImplementedError, PermissionError, ValueError):
        return None
