"""Relational LP construction: logical-plan IR and executors. **Internal.**

The public interface of the package is YAML (see ``linopy_yaml.api``).
Constructing Programs in Python is not supported API; a stable IR API may be
offered later.

This subpackage is the engine described in ARCHITECTURE.md, "The relational
lane". It must not import the
eager builder — the typed AST (and, in phase 2, hand-built IR programs) is
the only contract with the rest of the package. Engine dependencies (duckdb,
pyarrow, highspy) are imported lazily so the core package stays lean.

Only the execution surface is re-exported here. IR node classes live in
``linopy_yaml.relational.ir`` and are imported from there — so adding a node
no longer silently widens something that reads like public API, and the
import site says which layer the caller is reaching into.

``RelationalBuildError`` is kept as a deprecated alias only. The engine now
raises ``linopy_yaml.errors.LanguageError`` and ``DataError``; catch those.
"""

from linopy_yaml.relational.executor import (
    DuckdbExecutor,
    RelationalBuildError,
    Solution,
)

__all__ = [
    'DuckdbExecutor',
    'RelationalBuildError',
    'Solution',
]
