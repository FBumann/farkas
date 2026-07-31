"""The writer family: the tables in, a file out.

One module per format, and the **suffix** is how one is chosen — the caller
names an output, not a writer, because a file's format is a property of the
file. That is the whole difference from ``solvers/``, where the caller names
the solver and the model has no say either way.

Every member answers one shape::

    (tables, path) -> None

and streams: nothing here may materialise the model a second time. A format
that is coming but absent is declared in :data:`PLANNED_WRITERS` rather than
falling out as "unsupported", so the answer to ``write('m.mps')`` is what is
true — planned, tracked, not yet written — instead of a shrug.
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

#: Every format that can be written today, by suffix. Closed, for
#: :data:`~lpspec.relational.sinks.solvers.SOLVERS`' reason: what
#: ``lps.write(..., 'm.lp')`` produces may not depend on what else is
#: installed.
WRITERS: Mapping[str, Write] = {
    '.lp': write_lp_file,
}

#: Formats with a module coming, and where to read about it. Separate from
#: ``WRITERS`` because "not yet" and "no" are different answers and a caller
#: acts differently on each.
PLANNED_WRITERS: Mapping[str, str] = {
    '.mps': 'the mps writer is planned but not implemented yet (docs/ARCHITECTURE.md, sinks)',
}


def writer(suffix: str) -> Write:
    """The writer for *suffix*, which is how a format is chosen.

    Three outcomes, not two: a writer, a ``NotImplementedError`` naming a
    format that is coming, or a ``ValueError`` listing what can be written.
    """
    if suffix in WRITERS:
        return WRITERS[suffix]
    if suffix in PLANNED_WRITERS:
        raise NotImplementedError(PLANNED_WRITERS[suffix])
    supported = ', '.join(sorted(WRITERS))
    planned = ', '.join(sorted(PLANNED_WRITERS))
    raise ValueError(f'unsupported output format {suffix!r} — supported: {supported} (planned: {planned})')
