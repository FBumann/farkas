"""Relational LP construction: logical-plan IR and executors.

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
    Cmp,
    Const,
    ConstraintDecl,
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
    Sum,
    Var,
    VariableDecl,
)

__all__ = [
    "Add",
    "And",
    "Cmp",
    "Const",
    "ConstraintDecl",
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
    "Solution",
    "Sum",
    "Var",
    "VariableDecl",
]
