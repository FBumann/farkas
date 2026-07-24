"""The runner: bind data to a YAML model and execute it. Not a modeling API.

Math is defined in YAML only — there is no Python API for constructing
models, and the relational IR is internal (a stable IR-construction API may
come later). This module's job is exactly three verbs: ``build`` (YAML +
sources → live executor), ``solve``, and ``write_lp``.

This is the product path (ARCHITECTURE.md). The language is validated at load
time, lowered to the IR — anything outside the streaming subset raises
:class:`RelationalBuildError` naming the construct — and executed relationally
under a hard memory budget.

linopy exists only in the optional compatibility/oracle layer
(``import linopy_yaml.compat``) and in the differential test suite.

Example::

    import linopy_yaml as ly

    sol = ly.solve("model.yaml", sources={"p_max": "p_max.parquet", ...},
                   coords={"snapshot": range(8760)}, memory_limit="2GB")
    sol.objective
    sol.primal("p")          # tidy DataFrame (coords..., value)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml as _yaml

from linopy_yaml.lowering import lower_program, tidy_sources
from linopy_yaml.relational.executor import DuckdbExecutor, Solution
from linopy_yaml.schema import MathSchema
from linopy_yaml.validation import validate_expressions

if TYPE_CHECKING:
    from collections.abc import Mapping


def load_schema(model: str | Path | dict | MathSchema) -> MathSchema:
    """Load and validate a model definition.

    Accepts a YAML file path, an already-parsed dict, or a ``MathSchema``.
    Validation is complete at this point: schema shape, every expression and
    where string, every named expression and macro template.
    """
    if isinstance(model, MathSchema):
        schema = model
    elif isinstance(model, dict):
        schema = MathSchema(**model)
    else:
        raw = _yaml.safe_load(Path(model).read_text())
        schema = MathSchema(**(raw or {}))
    validate_expressions(schema)
    return schema


def build(
    model: str | Path | dict | MathSchema,
    sources: Mapping[str, Any],
    *,
    coords: dict[str, Any] | None = None,
    memory_limit: str = "1GB",
    chunk_rows: int = 2_000_000,
    threads: int | None = None,
    workdir: str | Path | None = None,
) -> DuckdbExecutor:
    """Build *model* on the streaming engine and return the live executor.

    ``sources`` maps parameter names to parquet paths, DataFrames, or Series
    (and optionally dimension names to index tables). The returned executor
    is a context manager: use ``with build(...) as ex:`` and call
    ``ex.solve()`` / ``ex.write_lp(path)``.

    Raises
    ------
    RelationalBuildError
        If the model uses a construct outside the streaming language —
        the message names the construct and its context.
    """
    schema = load_schema(model)
    program = lower_program(schema)  # strict: no fallback, errors carry the reason
    ex = DuckdbExecutor(
        memory_limit=memory_limit,
        chunk_rows=chunk_rows,
        threads=threads,
        workdir=workdir,
    )
    try:
        ex.build(program, tidy_sources(schema, dict(sources), coords))
    except BaseException:
        ex.close()
        raise
    return ex


def solve(
    model: str | Path | dict | MathSchema,
    sources: Mapping[str, Any],
    **build_kwargs: Any,
) -> Solution:
    """Build and solve in one call.

    The executor stays attached to the returned :class:`Solution` (its label
    tables back ``sol.primal(...)``); call ``sol.close()`` when done, or use
    :func:`build` with a ``with`` block for explicit lifetime control.
    """
    ex = build(model, sources, **build_kwargs)
    try:
        return ex.solve()
    except BaseException:
        ex.close()
        raise


def write_lp(
    model: str | Path | dict | MathSchema,
    sources: Mapping[str, Any],
    out: str | Path,
    **build_kwargs: Any,
) -> Path:
    """Build and stream an LP file in one call."""
    with build(model, sources, **build_kwargs) as ex:
        ex.write_lp(out)
    return Path(out)
