"""Declarative optimisation: YAML math on a streaming engine.

Models build relationally on polars and stream to the solver — see
docs/ARCHITECTURE.md. linopy is not imported at runtime; it serves as the
differential-test oracle and as an opt-in compatibility shim
(``from lpspec import linopy as lpspec_linopy``).

Example::

    import lpspec as lps

    result = lps.solve('model.yaml', {'p_max': 'p_max.parquet', 'load': 'load.parquet'})
    result.objective
    result.primal('p')  # tidy polars.DataFrame
    result.to_dataarray('p')  # labelled, for array post-processing
"""

from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _installed_version

from lpspec.api import build, check, load_schema, solve, write
from lpspec.errors import (
    DataError,
    DimensionError,
    LanguageError,
    LinopyYamlError,
    PiecewiseExpansionError,
    SchemaError,
)
from lpspec.language.schema import MathSchema
from lpspec.typeset import SymbolTable, to_latex, to_markdown, to_typst

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
    __version__ = _installed_version('lpspec')
except _PackageNotFoundError:  # running from a source tree with nothing installed
    __version__ = '0.0.0'
