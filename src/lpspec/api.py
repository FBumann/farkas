"""The runner: bind data to a YAML model and execute it. Not a modeling API.

Math is defined in YAML only — there is no Python API for constructing
models, and the logical plan is internal (a stable plan-construction API may
come later). This module's job is exactly three verbs: ``build`` (YAML +
sources → live executor), ``solve``, and ``write``.

This is the product path (docs/ARCHITECTURE.md). The language is validated at load
time, lowered to the plan — anything outside the streaming subset raises
:class:`~lpspec.errors.LanguageError` naming the construct — and executed
relationally.

linopy exists only in the optional compatibility/oracle layer
(``import lpspec.linopy``) and in the differential test suite.

Example::

    import lpspec as lps

    result = lps.solve(
        'model.yaml',
        {'p_max': 'p_max.parquet', 'load': 'load.parquet'},
        coords={'snapshot': range(8760)},
    )
    result.objective
    result.primal('p')  # tidy polars.DataFrame (coords..., value)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from lpspec._yaml import read_yaml
from lpspec.lowering import lower_program
from lpspec.piecewise import expand_piecewise
from lpspec.relational.executor import PolarsExecutor
from lpspec.schema import MathSchema
from lpspec.sources import tidy_sources
from lpspec.validation import validate_expressions

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lpspec.relational.result import Result


def load_schema(model: str | Path | dict[str, Any] | MathSchema) -> MathSchema:
    """Load and validate a model definition.

    Accepts a YAML file path, an already-parsed dict, or a ``MathSchema``.
    Validation is complete at this point: schema shape, every expression and
    where string, every named expression and macro template — and every
    declaration a formulation emits, since those are language too. That is why
    expansion runs *before* validation here, the order the linopy lane already
    uses: validating the file as written checks a strict subset of the model
    that gets built.

    Returns the schema *as the file declares it*, with ``piecewise:`` blocks
    intact — expansion is idempotent and each lane redoes it, while the
    curvature data guard needs the blocks themselves
    (``validate_piecewise_data``).
    """
    if isinstance(model, (list, tuple)):
        msg = (
            'composing multiple YAML files into one program is not implemented '
            'yet — track https://github.com/FBumann/lpspec/issues/30'
        )
        raise NotImplementedError(msg)
    if isinstance(model, MathSchema):
        schema = model
    elif isinstance(model, dict):
        schema = MathSchema(**model)
    else:
        schema = MathSchema(**read_yaml(Path(model)))
    validate_expressions(expand_piecewise(schema))
    return schema


def check(model: str | Path | dict[str, Any] | MathSchema) -> MathSchema:
    """Compile-check a model without data: parse, expand, validate, lower.

    Lowering needs no sources, so this works on a bare YAML file — the CI
    verb for model repositories. Raises :class:`LanguageError` when the model
    uses a construct outside the streaming language, ``ValueError`` for
    schema/expression problems. Returns the validated schema.
    """
    schema = load_schema(model)
    lower_program(schema)
    return schema


def build(
    model: str | Path | dict[str, Any] | MathSchema,
    sources: Mapping[str, Any],
    *,
    coords: dict[str, Any] | None = None,
) -> PolarsExecutor:
    """Build *model* on the relational engine and return the executor.

    ``sources`` maps parameter names to parquet paths or in-memory tables (and
    optionally dimension names to index tables). One build can feed more than
    one sink: call ``ex.solve()`` and ``ex.write_lp(path)`` on the same object.

    Raises
    ------
    LanguageError
        If the model uses a construct outside the streaming language —
        the message names the construct and its context.
    """
    schema = load_schema(model)
    program = lower_program(schema)  # strict: no fallback, errors carry the reason
    ex = PolarsExecutor()
    try:
        ex.build(program, tidy_sources(schema, dict(sources), coords))
    except BaseException:
        ex.close()
        raise
    return ex


def solve(
    model: str | Path | dict[str, Any] | MathSchema,
    sources: Mapping[str, Any],
    solver_options: Mapping[str, Any] | None = None,
    **build_kwargs: Any,
) -> Result:
    """Build and solve in one call.

    ``solver_options`` is forwarded verbatim to the solver — the same shape
    linopy takes, e.g. ``{'time_limit': 60, 'mip_rel_gap': 0.01}``. Build
    options stay separate, because they govern *construction* and never reach
    the solver.

    The executor stays attached to the returned :class:`Result`, whose label
    frames back ``result.primal(...)``. Nothing has to be released, though
    ``result.close()`` drops a large model early if you want the memory back.
    """
    ex = build(model, sources, **build_kwargs)
    try:
        return ex.solve(solver_options=solver_options)
    except BaseException:
        ex.close()
        raise


def write(
    model: str | Path | dict[str, Any] | MathSchema,
    sources: Mapping[str, Any],
    out: str | Path,
    **build_kwargs: Any,
) -> Path:
    """Build and stream the model to a file; format from the suffix.

    ``.lp`` is supported today; ``.mps`` is planned (same streaming
    mechanics, see docs/ARCHITECTURE.md sinks).
    """
    out = Path(out)
    suffix = out.suffix.lower()
    if suffix == '.lp':
        with build(model, sources, **build_kwargs) as ex:
            ex.write_lp(out)
        return out
    if suffix == '.mps':
        msg = 'the mps sink is planned but not implemented yet (docs/ARCHITECTURE.md, sinks)'
        raise NotImplementedError(msg)
    msg = f"unsupported output format '{suffix}' — supported: .lp (planned: .mps)"
    raise ValueError(msg)
