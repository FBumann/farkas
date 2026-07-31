"""Sinks: how a built model leaves the engine. See README.md.

**Two families, and that is the whole mental model.** A *solver* takes the
tables and runs them (``solvers/``, chosen by name); a *writer* takes the
tables and renders them to a file (``writers/``, chosen by suffix). The
families are directories rather than a convention, so
``tests/test_architecture.py`` reads membership off the path.

``tables.py`` is what both read, and neither family imports the other. This
module is the seam a caller uses: the contract, and the two lookups.
"""

from lpspec.relational.sinks.solvers import SOLVERS, solver
from lpspec.relational.sinks.tables import COLS, DTYPES, MATRIX, OBJ, ROWS, VTYPE, ModelTables
from lpspec.relational.sinks.writers import PLANNED_WRITERS, WRITERS, writer

__all__ = [
    'COLS',
    'DTYPES',
    'MATRIX',
    'OBJ',
    'PLANNED_WRITERS',
    'ROWS',
    'SOLVERS',
    'VTYPE',
    'WRITERS',
    'ModelTables',
    'solver',
    'writer',
]
