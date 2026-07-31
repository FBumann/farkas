"""Relational LP construction: the logical plan and its executor. **Internal.**

The public interface of the package is YAML (see ``lpspec.api``).
Constructing Programs in Python is not supported API; a stable plan API may be
offered later.

This subpackage is the engine described in docs/ARCHITECTURE.md, "The relational
lane". It must not import the eager builder — the typed AST (and, in phase 2,
hand-built plans) is the only contract with the rest of the package. Engine
dependencies (polars, highspy) are imported lazily so the core package stays
lean.

Only the execution surface is re-exported here. Plan node classes live in
``lpspec.relational.plan`` and are imported from there — so adding a node
no longer silently widens something that reads like public API, and the
import site says which layer the caller is reaching into.

``RelationalBuildError`` is kept as a deprecated alias only. The engine now
raises ``lpspec.errors.LanguageError`` and ``DataError``; catch those.
"""

from lpspec.relational.executor import PolarsExecutor, RelationalBuildError
from lpspec.relational.result import Result

__all__ = [
    'PolarsExecutor',
    'RelationalBuildError',
    'Result',
]
