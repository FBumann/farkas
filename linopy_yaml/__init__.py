"""Declarative optimisation: YAML math on a streaming engine.

Models build relationally (duckdb under a hard ``memory_limit``) and stream
to the solver — see ARCHITECTURE.md. linopy is not imported at runtime; it
serves as the differential-test oracle and as an opt-in compatibility layer
(``import linopy_yaml.compat`` patches ``linopy.Model.from_yaml``).

    import linopy_yaml as ly

    sol = ly.solve("model.yaml", sources={"p_max": "p_max.parquet", ...})
    sol.objective
    sol.primal("p")
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

from linopy_yaml.api import LanguageError, build, check, load_schema, solve, write, write_lp
from linopy_yaml.schema import MathSchema

__all__ = [
    'LanguageError',
    'MathSchema',
    'build',
    'check',
    'load_schema',
    'solve',
    'write',
    'write_lp',
]

try:
    # the git tag is the source of truth; hatch-vcs bakes it into the metadata
    __version__ = _installed_version('linopy-yaml')
except PackageNotFoundError:  # running from a source tree with nothing installed
    __version__ = '0.0.0'
