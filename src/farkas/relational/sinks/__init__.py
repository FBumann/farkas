"""Sinks: how a built model leaves the engine.

ARCHITECTURE.md's pipeline draws two boxes downstream of the executor. This
package is those boxes — one module each, because that is where the fences
are: ``highspy`` is an optional dependency of ``solver_direct`` alone, and a
caller that only writes LP files should never import it.

A sink reads :class:`ModelTables` and nothing else. Neither sink knows how the
tables were filled, and the executor does not know how they are drained, which
is what makes the planned ``mps`` sink a new module here rather than another
method on the executor.
"""

from farkas.relational.sinks.highs import solve_direct
from farkas.relational.sinks.lp_file import write_lp_file
from farkas.relational.sinks.tables import ModelTables

__all__ = [
    'ModelTables',
    'solve_direct',
    'write_lp_file',
]
