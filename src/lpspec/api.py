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

from lpspec.language.validation import load_schema
from lpspec.lowering import lower_program
from lpspec.relational.engines import resolve as resolve_engine
from lpspec.relational.sinks import solver, writer
from lpspec.sources import tidy_sources

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lpspec.language.schema import MathSchema
    from lpspec.relational.engine import Engine
    from lpspec.relational.result import Result

#: Re-exported: parsing and validating a model is the *language's* job, and a
#: consumer that binds no data (``typeset``) must be able to reach it without
#: reaching the runner. Callers keep saying ``lps.load_schema``.
__all__ = ['build', 'check', 'load_schema', 'solve', 'write']


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
) -> Engine:
    """Build *model* on the relational engine and return the executor.

    ``sources`` maps parameter names to parquet paths or in-memory tables (and
    optionally dimension names to index tables). One build can feed more than
    one sink: call ``ex.solve()`` and ``ex.write(path)`` on the same object.

    Which engine builds it is set by ``LPSPEC_ENGINE`` and is deliberately not
    a parameter here: the engines produce the same model integer for integer,
    so the choice cannot change the answer, only what computing it costs. A
    knob that cannot change the answer does not belong in the call that
    produces one — see :mod:`lpspec.relational.engines`.

    Raises
    ------
    LanguageError
        If the model uses a construct outside the streaming language —
        the message names the construct and its context.
    """
    schema = load_schema(model)
    program = lower_program(schema)  # strict: no fallback, errors carry the reason
    ex = resolve_engine()()
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
    solver_name: str = 'highs',
    **build_kwargs: Any,
) -> Result:
    """Build and solve in one call.

    ``solver_name`` is which solver sink to hand the built model to —
    ``highs``, which ships with the package, or ``gurobi``, which needs the
    ``[gurobi]`` extra. linopy's spelling, and a decision the *caller* makes:
    the same file solves the same model either way, so nothing in the YAML
    names a solver.

    ``solver_options`` is forwarded verbatim to it — the same shape linopy
    takes, e.g. ``{'time_limit': 60, 'mip_rel_gap': 0.01}``, in whichever
    solver's vocabulary was chosen. Build options stay separate, because they
    govern *construction* and never reach the solver.

    The executor stays attached to the returned :class:`Result`, whose label
    frames back ``result.primal(...)``. Nothing has to be released, though
    ``result.close()`` drops a large model early if you want the memory back.
    """
    solver(solver_name)  # before the build, for the reason `write` checks the suffix first
    ex = build(model, sources, **build_kwargs)
    try:
        return ex.solve(solver_options=solver_options, solver_name=solver_name)
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

    ``.lp`` is supported today and ``.mps`` is planned, both answered by the
    writer family rather than by a branch here — this verb owns *when* to
    build, not what can be written.

    The suffix is checked **before** the build, because a caller who named a
    format nothing can write should not pay for a model first.
    """
    out = Path(out)
    writer(out.suffix.lower())
    with build(model, sources, **build_kwargs) as ex:
        ex.write(out)
    return out
