"""Sinks: how a built model leaves the engine.

**Two families, and that is the whole mental model.** A *solver* takes the
tables and runs them; a *writer* takes the tables and renders them to a file.
Everything else about a sink follows from which of the two it is — how it is
chosen (by name at the call, or by the output's suffix), what it returns
(an answer, or nothing), and where its module lives.

The families are directories, not a convention: ``solvers/`` holds one module
per solver and ``writers/`` one per format, and
``tests/test_architecture.py`` reads the family off the path. Which is what
makes the growing one cheap — a new solver is a module and a line in
``SOLVERS``, with nothing above it to teach.

``tables.py`` is what both read, and neither family imports the other. This
module is the seam a caller uses: the contract, and the two lookups.
"""

from lpspec.relational.sinks.solvers import SOLVERS, solver
from lpspec.relational.sinks.tables import ModelTables
from lpspec.relational.sinks.writers import PLANNED_WRITERS, WRITERS, writer

__all__ = [
    'PLANNED_WRITERS',
    'SOLVERS',
    'WRITERS',
    'ModelTables',
    'solver',
    'writer',
]
