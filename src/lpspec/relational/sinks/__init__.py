"""Sinks: how a built model leaves the engine.

docs/ARCHITECTURE.md's pipeline draws two boxes downstream of the executor. This
package is those boxes — one module each, because that is where the fences
are: ``highspy`` is an optional dependency of ``solver_direct`` alone, and a
caller that only writes LP files should never import it.

A sink reads :class:`ModelTables` and nothing else. Neither sink knows how the
tables were filled, and the executor does not know how they are drained, which
is what makes the planned ``mps`` sink a new module here rather than another
method on the executor.
"""

from lpspec.relational.sinks.highs import build_highs, solve_direct
from lpspec.relational.sinks.lp_file import write_lp_file
from lpspec.relational.sinks.tables import COLS, DTYPES, MATRIX, OBJ, ROWS, VTYPE, ModelTables

__all__ = [
    'COLS',
    'DTYPES',
    'MATRIX',
    'OBJ',
    'ROWS',
    'VTYPE',
    'ModelTables',
    'build_highs',
    'solve_direct',
    'write_lp_file',
]
