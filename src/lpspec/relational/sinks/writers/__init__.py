"""The writer family: the tables in, a file out. See ../README.md.

One module per format, chosen by the output's **suffix** — the caller names an
output, not a writer, because a file's format is a property of the file. Each
answers ``(tables, path) -> None``, and streams.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lpspec.relational.sinks.writers.lp_file import write_lp_file

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from lpspec.relational.sinks.tables import ModelTables

    Write = Callable[[ModelTables, Path], None]

__all__ = ['PLANNED_WRITERS', 'WRITERS', 'write_lp_file', 'writer']

#: What can be written today, by suffix. Closed, for
#: :data:`~lpspec.relational.sinks.solvers.SOLVERS`' reason.
WRITERS: Mapping[str, Write] = {'.lp': write_lp_file}

#: Formats with a module coming. Separate from :data:`WRITERS` because "not
#: yet" and "no" are different answers and a caller acts differently on each.
PLANNED_WRITERS: Mapping[str, str] = {
    '.mps': 'the mps writer is planned but not implemented yet (docs/ARCHITECTURE.md, sinks)',
}


def writer(suffix: str) -> Write:
    """The writer for *suffix* — or a ``NotImplementedError`` naming a format
    that is coming, or a ``ValueError`` listing what can be written."""
    if suffix in WRITERS:
        return WRITERS[suffix]
    if suffix in PLANNED_WRITERS:
        raise NotImplementedError(PLANNED_WRITERS[suffix])
    supported = ', '.join(sorted(WRITERS))
    planned = ', '.join(sorted(PLANNED_WRITERS))
    raise ValueError(f'unsupported output format {suffix!r} — supported: {supported} (planned: {planned})')
