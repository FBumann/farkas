"""Declarative optimisation: YAML math on a streaming engine.

Models build relationally on polars and stream to the solver — see
docs/ARCHITECTURE.md. linopy is not imported at runtime; it serves as the
differential-test oracle and as an opt-in compatibility shim
(``from farkas import linopy as farkas_linopy``).

Example::

    import farkas as fk

    result = fk.solve('model.yaml', {'p_max': 'p_max.parquet', 'load': 'load.parquet'})
    result.objective
    result.primal('p')  # tidy polars.DataFrame
    result.to_dataarray('p')  # labelled, for array post-processing
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

from farkas.api import build, check, load_schema, solve, write
from farkas.errors import (
    DataError,
    DimensionError,
    LanguageError,
    LinopyYamlError,
    PiecewiseExpansionError,
    SchemaError,
)
from farkas.schema import MathSchema
from farkas.typeset import SymbolTable, to_latex, to_markdown, to_typst

__all__ = [
    'DataError',
    'DimensionError',
    'LanguageError',
    'LinopyYamlError',
    'MathSchema',
    'PiecewiseExpansionError',
    'SchemaError',
    'SymbolTable',
    'build',
    'check',
    'load_schema',
    'solve',
    'to_latex',
    'to_markdown',
    'to_typst',
    'write',
]

try:
    # the git tag is the source of truth; hatch-vcs bakes it into the metadata
    __version__ = _installed_version('farkas')
except PackageNotFoundError:  # running from a source tree with nothing installed
    __version__ = '0.0.0'
