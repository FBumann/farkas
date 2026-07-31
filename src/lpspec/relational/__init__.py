"""Relational LP construction: the logical plan and its executor. **Internal.**

The public interface of the package is YAML (see ``lpspec.api``).
Constructing Programs in Python is not supported API; a stable plan API may be
offered later.

This subpackage is the relational lane described in docs/ARCHITECTURE.md. It
must not import the eager builder — the typed AST (and, in phase 2, hand-built
plans) is the only contract with the rest of the package. Engine dependencies
(polars, highspy) are imported lazily so the core package stays lean.

**Two layers, and the directory says which is which.** ``plan.py``,
``engine.py``, ``sinks/``, ``status.py``, ``chunking.py`` and ``frames.py`` are
the contract: what a model *is*, what an engine answers to, what a sink reads.
``engines/`` holds implementations of that contract, one per directory. The
split was made when a second engine was priced (``bench/duckdb-spike.md``) —
the boundary was always real, but with one engine a reader had to already know
which of eleven modules were which.

Only the execution surface is re-exported here. Plan node classes live in
``lpspec.relational.plan`` and are imported from there — so adding a node
no longer silently widens something that reads like public API, and the
import site says which layer the caller is reaching into.

``RelationalBuildError`` is kept as a deprecated alias only. The engine now
raises ``lpspec.errors.LanguageError`` and ``DataError``; catch those.
"""

from lpspec.relational.engines.polars import PolarsExecutor, RelationalBuildError
from lpspec.relational.result import Result

__all__ = [
    'PolarsExecutor',
    'RelationalBuildError',
    'Result',
]
