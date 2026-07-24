"""Relational LP construction: logical-plan IR and executors. **Internal.**

The public interface of the package is YAML (see ``linopy_yaml.api``). The
IR names exported here are implementation surface for the executor and the
test suite — constructing Programs in Python is not supported API; a stable
IR API may be offered later.

This subpackage is the engine behind SPEC.md §12. It must not import the
eager builder — the typed AST (and, in phase 2, hand-built IR programs) is
the only contract with the rest of the package. Engine dependencies (duckdb,
pyarrow, highspy) are imported lazily so the core package stays lean.
"""

from linopy_yaml.relational.executor import (
    DuckdbExecutor,
    RelationalBuildError,
    Solution,
)
from linopy_yaml.relational.ir import (
    Add,
    And,
    Bool,
    Cmp,
    Const,
    ConstraintDecl,
    Defined,
    Div,
    Expr,
    GroupSum,
    Mul,
    Neg,
    Not,
    ObjectiveDecl,
    Or,
    Param,
    ParameterDecl,
    Pred,
    Program,
    Shift,
    Sum,
    Var,
    VariableDecl,
)

__all__ = [
    "Add",
    "And",
    "Bool",
    "Cmp",
    "Const",
    "ConstraintDecl",
    "Defined",
    "Div",
    "DuckdbExecutor",
    "Expr",
    "GroupSum",
    "Mul",
    "Neg",
    "Not",
    "ObjectiveDecl",
    "Or",
    "Param",
    "ParameterDecl",
    "Pred",
    "Program",
    "RelationalBuildError",
    "Shift",
    "Solution",
    "Sum",
    "Var",
    "VariableDecl",
]
