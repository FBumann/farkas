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

from linopy_yaml.api import build, load_schema, solve, write_lp
from linopy_yaml.helpers import register
from linopy_yaml.schema import MathSchema

__all__ = [
    'MathSchema',
    'build',
    'load_schema',
    'register',
    'solve',
    'write_lp',
]
__version__ = '0.0.2'
