"""A small cross-process file lock (fcntl on POSIX, msvcrt on Windows).

Several MCP servers can run at once on one machine (Claude Desktop and Claude
Code, say) and they share ``~/.resolve-pilot``; thread locks alone don't keep
them from overwriting each other's bridge requests or plan edits.
"""
from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockTimeout(TimeoutError):
    """The lock was still held by someone else when the timeout ran out."""


@contextmanager
def file_lock(path: Path, timeout: float) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` (created if needed) for the block."""
    with open(path, "a+b") as f:
        if os.name == "nt":
            import msvcrt

            def try_lock() -> None:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

            def unlock() -> None:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def try_lock() -> None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def unlock() -> None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                try_lock()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"{path.name} is held by another process") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            unlock()
