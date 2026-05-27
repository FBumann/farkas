"""Helpers for attaching context to exceptions via ``Exception.add_note``."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def note(msg: str) -> Iterator[None]:
    """Attach *msg* as a note to any exception raised inside the block.

    The original exception is re-raised unchanged (same type, same args), so
    callers matching on exception class or message keep working. Notes stack
    naturally when blocks are nested.
    """
    try:
        yield
    except Exception as exc:
        exc.add_note(msg)
        raise
