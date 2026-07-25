"""The runner: bind data to a YAML model and execute it. Not a modeling API.

Math is defined in YAML only — there is no Python API for constructing
models, and the relational IR is internal (a stable IR-construction API may
come later). This module's job is exactly three verbs: ``build`` (YAML +
sources → live executor), ``solve``, and ``write``.

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

from pydantic import ValidationError

from linopy_yaml._source import SourceMap, annotate, read_yaml
from linopy_yaml.dimensions import check_schema
from linopy_yaml.lowering import lower_program, tidy_sources
from linopy_yaml.relational.executor import DuckdbExecutor, RelationalBuildError, Solution
from linopy_yaml.schema import MathSchema
from linopy_yaml.validation import validate_expressions

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Public name for "this model is outside the streaming language". The
#: exception class is shared with the engine; callers should catch this alias.
LanguageError = RelationalBuildError


def load_schema(model: str | Path | dict[str, Any] | MathSchema) -> MathSchema:
    """Load and validate a model definition.

    Accepts a YAML file path, an already-parsed dict, or a ``MathSchema``.
    Validation is complete at this point: schema shape, every expression and
    where string, every named expression and macro template.
    """
    if isinstance(model, (list, tuple)):
        msg = (
            'composing multiple YAML files into one program is not implemented '
            'yet — track https://github.com/FBumann/linopy-yaml/issues/30'
        )
        raise NotImplementedError(msg)
    if isinstance(model, MathSchema):
        return _validated(model, SourceMap.none())
    if isinstance(model, dict):
        return _validated(_schema_from(model, SourceMap.none()), SourceMap.none())
    raw, source = read_yaml(Path(model))
    return _validated(_schema_from(raw, source), source)


def _schema_from(raw: dict[str, Any], source: SourceMap) -> MathSchema:
    """Build the schema, re-raising shape errors with the line they came from."""
    try:
        return MathSchema(**raw)
    except ValidationError as exc:
        raise ValueError(annotate(exc.errors(), source)) from exc


def _validated(schema: MathSchema, source: SourceMap) -> MathSchema:
    validate_expressions(schema, source=source)
    check_schema(schema, source=source)
    return schema


def check(model: str | Path | dict[str, Any] | MathSchema) -> MathSchema:
    """Compile-check a model without data: parse, validate, expand, lower.

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
    memory_limit: str = '1GB',
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
    model: str | Path | dict[str, Any] | MathSchema,
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


def write(
    model: str | Path | dict[str, Any] | MathSchema,
    sources: Mapping[str, Any],
    out: str | Path,
    **build_kwargs: Any,
) -> Path:
    """Build and stream the model to a file; format from the suffix.

    ``.lp`` is supported today; ``.mps`` is planned (same streaming
    mechanics, see ARCHITECTURE.md sinks).
    """
    out = Path(out)
    suffix = out.suffix.lower()
    if suffix == '.lp':
        with build(model, sources, **build_kwargs) as ex:
            ex.write_lp(out)
        return out
    if suffix == '.mps':
        msg = 'the mps sink is planned but not implemented yet (ARCHITECTURE.md, sinks)'
        raise NotImplementedError(msg)
    msg = f"unsupported output format '{suffix}' — supported: .lp (planned: .mps)"
    raise ValueError(msg)
