"""Process-scoped advisory locks. Never hold a job lock across host LLM calls."""

from __future__ import annotations

import hashlib
import inspect
import os
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from by2kb.errors import ConfigError


@contextmanager
def exclusive_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ConfigError(
                    "Enrichment is busy; retry after the current operation finishes."
                ) from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ConfigError(
                    "Enrichment is busy; retry after the current operation finishes."
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def serialized_enrichment(function):
    def lock(config, job_id):
        key = hashlib.sha256(job_id.encode()).hexdigest()
        return exclusive_file(config.home / "locks" / f"enrichment-{key}.lock")

    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def asynchronous(config, job_id, *args, **kwargs):
            with lock(config, job_id):
                return await function(config, job_id, *args, **kwargs)
        return asynchronous

    @wraps(function)
    def synchronous(config, job_id, *args, **kwargs):
        with lock(config, job_id):
            return function(config, job_id, *args, **kwargs)
    return synchronous
