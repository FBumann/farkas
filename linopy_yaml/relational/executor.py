"""Duckdb executor for the logical-plan IR.

The lane is described in ARCHITECTURE.md, "The relational lane".

Compiles a :class:`~linopy_yaml.relational.ir.Program` into tidy tables inside
a file-backed duckdb database under a hard ``memory_limit``, then streams the
model out through a sink: ``write_lp`` (portability / differential oracle) or
``solve`` (solver_direct — batched HiGHS ``addCols``/``addRows``; the full
model never exists in this process's memory).

Hand-managed chunking exists in exactly two places, both forced by operators
duckdb cannot spill:

1. Label assignment — a global ``ROW_NUMBER`` window materialises its whole
   input, so labels are assigned per-chunk of the leading dim with a running
   offset. This is one generic mechanism; every operator inherits it.
2. LP-text ``string_agg`` in ``write_lp`` — string aggregates don't spill,
   and a fixed conservative chunk size costs nothing in the debugging sink.

Everything else — joins, scaling, masks, and the numeric hash aggregates that
assemble ``A`` — delegates to duckdb's own spilling under ``memory_limit``.
Future IR operators should be classified by coordinate locality: pointwise
(joins/masks/group_sum) and bounded-halo (roll: t±k, which still works under
label chunking because terms join the *global* variable table) compose freely;
genuinely global operators (running sums, normalisations) must be rejected at
lowering with a rewrite hint (e.g. running sum → state-variable recurrence).

duckdb, pyarrow, and highspy are imported lazily; pandas is a core dep.
"""

from __future__ import annotations

import contextlib
import math
import re
import shutil
import tempfile
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from linopy_yaml.errors import DataError, LanguageError, LinopyYamlError
from linopy_yaml.relational import ir

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pandas as pd

_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

_COPY_OPTS = "(FORMAT csv, HEADER false, QUOTE '', ESCAPE '')"


#: Deprecated. The engine's failures are now split between
#: :class:`~linopy_yaml.errors.LanguageError` (the program says something the
#: engine cannot build) and :class:`~linopy_yaml.errors.DataError` (a source is
#: missing or the wrong shape). This alias is their common base, so an existing
#: ``except RelationalBuildError`` keeps catching everything it used to.
RelationalBuildError = LinopyYamlError


@dataclass(frozen=True)
class _TermFragment:
    """One additive piece of a compiled affine expression.

    ``sql`` is a full SELECT. Term pieces yield ``(dims…, var_label, coeff)``;
    const pieces yield ``(dims…, cval)``.
    """

    dims: tuple[str, ...]
    sql: str
    is_term: bool


@dataclass(frozen=True)
class _CompiledExpression:
    terms: tuple[_TermFragment, ...]
    consts: tuple[_TermFragment, ...]


@dataclass
class Solution:
    """Solve result. ``primal(name)`` joins labels back to coords.

    Tethered to its executor's label tables — use it as a context manager
    (``with ly.solve(...) as sol:``) or call :meth:`close`. For big models,
    :meth:`to_parquet` streams every variable's tidy solution table to disk
    without materialising any of them in memory.
    """

    status: str
    objective: float
    _executor: DuckdbExecutor

    def primal(self, name: str) -> pd.DataFrame:
        return self._executor._primal(name)

    def to_dataarray(self, name: str) -> Any:
        """``primal(name)`` as a labelled :class:`xarray.DataArray`.

        Named for what it returns, pairing with :meth:`to_dataset` — pandas'
        ``to_xarray`` returns either type depending on the receiver, and that
        ambiguity is not worth inheriting when both forms exist here.

        Long tables are the right shape for slicing and joining, and the wrong
        one for the array math post-processing is mostly made of —
        ``.sel(generator='wind')``, resampling, duration curves. This is the
        bridge, and it is one line so that its absence never reads as "results
        are hard to use".

        Goes through ``pandas.DataFrame.to_xarray()`` rather than importing
        xarray here: the streaming lane is xarray-free (hard rule 2), and
        pandas does the optional import for us. Requires xarray to be
        installed (it ships with the ``[compat]`` extra); missing coordinate
        combinations come back as NaN, since a masked variable has no row.
        """
        frame = self.primal(name)
        dims = [c for c in frame.columns if c != 'value']
        if not dims:
            return frame['value'].to_xarray().rename(name)
        # the tidy column is 'value'; the array should carry the variable's name
        return frame.set_index(dims).to_xarray()['value'].rename(name)

    def to_dataset(self, *names: str) -> Any:
        """Variables as one :class:`xarray.Dataset`; all of them by default.

        A small model wants every variable at once — that is what linopy's
        ``model.solution`` gives you, and naming them would be busywork.

        Costs what it says: each variable arrives *dense* over its own dims, so
        the Dataset holds the full coordinate product regardless of what the
        mask removed, and all of them at once. On a model built for the memory
        budget this engine exists for, name the few you need or use
        :meth:`to_parquet`, which streams.
        """
        assert self._executor._program is not None
        wanted = names or tuple(v.name for v in self._executor._program.variables)
        first, *rest = wanted
        dataset = self.to_dataarray(first).to_dataset(name=first)
        for name in rest:
            dataset[name] = self.to_dataarray(name)
        return dataset

    def to_parquet(self, directory: str | Path) -> dict[str, Path]:
        """Stream per-variable solution tables to ``directory`` (one parquet
        file per variable, columns ``(dims..., value)``). Returns name → path.

        The sink runs inside duckdb (COPY), so the full solution never passes
        through this process's memory. Other formats may follow the same
        shape (``to_csv``, ...) — parquet is the canonical one.
        """
        return self._executor._solution_to_parquet(Path(directory))

    def close(self) -> None:
        """Release the executor backing this solution's label tables."""
        self._executor.close()

    def __enter__(self) -> Solution:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        self.close()
        return False


class DuckdbExecutor:
    """Build and sink a :class:`Program` relationally under a memory budget."""

    def __init__(
        self,
        memory_limit: str = '1GB',
        chunk_rows: int = 2_000_000,
        threads: int | None = None,
        workdir: str | Path | None = None,
    ) -> None:
        import duckdb

        self.memory_limit = memory_limit
        self.chunk_rows = chunk_rows
        self._own_workdir = workdir is None
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix='relational_'))
        self.workdir.mkdir(parents=True, exist_ok=True)

        self._con = duckdb.connect(str(self.workdir / 'model.duckdb'))
        self._con.execute(f"SET memory_limit='{memory_limit}'")
        self._con.execute(f"SET temp_directory='{self.workdir / 'tmp'}'")
        self._con.execute('SET preserve_insertion_order=false')
        if threads:
            self._con.execute(f'SET threads={threads}')

        # safety net: temp state is released even if the caller forgets close()
        self._finalizer = weakref.finalize(self, _release, self._con, self.workdir if self._own_workdir else None)

        self._program: ir.Program | None = None
        self._dim_card: dict[str, int] = {}
        self._n_cols = 0
        self._n_rows = 0
        self._obj_const = 0.0
        self._obj_sense: str = 'min'

    # ------------------------------------------------------------------
    # build
    # ------------------------------------------------------------------

    def build(self, program: ir.Program, sources: Mapping[str, Any]) -> None:
        """Load sources, create dim/parameter tables, variables, constraints."""
        self._program = program
        self._validate_names(program)

        for p in program.parameters:
            self._create_param_table(p, sources)
        self._create_dim_tables(program, sources)

        self._con.execute('CREATE TABLE cols (col BIGINT, lb DOUBLE, ub DOUBLE, vtype VARCHAR)')
        self._con.execute('CREATE TABLE obj (col BIGINT, coeff DOUBLE)')
        self._con.execute('CREATE TABLE rows (row BIGINT, sense VARCHAR, rhs DOUBLE)')
        self._con.execute('CREATE TABLE A (row BIGINT, col BIGINT, coeff DOUBLE)')

        for v in program.variables:
            self._build_variable(v)
        for c in program.constraints:
            self._build_constraint(c)
        self._build_objective(program.objective)

    def _validate_names(self, program: ir.Program) -> None:
        names = (
            [p.name for p in program.parameters]
            + [v.name for v in program.variables]
            + [c.name for c in program.constraints]
            + [d for p in program.parameters for d in p.dims]
            + [d for v in program.variables for d in v.dims]
        )
        for n in names:
            if not _IDENT.match(n):
                raise LanguageError(f"name '{n}' is not a valid identifier ([A-Za-z_][A-Za-z0-9_]*)")

    def _create_param_table(self, p: ir.ParameterDeclaration, sources: Mapping[str, Any]) -> None:
        if p.name not in sources:
            raise DataError(f"no source bound for parameter '{p.name}'")
        rel = self._source_relation(p.name, sources[p.name])
        cols = [*p.dims, 'value']
        missing = set(cols) - set(self._relation_columns(rel))
        if missing:
            raise DataError(
                f"source for parameter '{p.name}' is missing columns {sorted(missing)} "
                f"(need dims {list(p.dims)} plus 'value')"
            )
        collist = ', '.join(cols)
        self._con.execute(f'CREATE TABLE p_{p.name} AS SELECT {collist} FROM {rel}')

    def _source_relation(self, name: str, source: Any) -> str:
        import pandas as pd

        if isinstance(source, (str, Path)):
            return f"read_parquet('{source}')"
        if isinstance(source, pd.Series):
            source = source.rename('value').reset_index()
        if isinstance(source, pd.DataFrame):
            self._con.register(f'src_{name}', source)
            return f'src_{name}'
        raise DataError(
            f"source for '{name}' must be a parquet path, DataFrame, or Series (got {type(source).__name__})"
        )

    def _relation_columns(self, rel: str) -> list[str]:
        return [d[0] for d in self._con.execute(f'SELECT * FROM {rel} LIMIT 0').description]

    def _scalar(self, sql: str):
        row = self._con.execute(sql).fetchone()
        assert row is not None
        return row[0]

    def _create_dim_tables(self, program: ir.Program, sources: Mapping[str, Any]) -> None:
        assert self._program is not None
        dims: set[str] = set()
        for v in program.variables:
            dims.update(v.dims)
        for c in program.constraints:
            dims.update(c.dims)
        for p in program.parameters:
            dims.update(p.dims)

        for d in sorted(dims):
            if d in sources:
                # explicit index: ordinals follow declared/coords order, so
                # Shift's positional semantics match xarray/linopy exactly
                # even for non-monotonic or string coordinates
                self._create_explicit_dim_table(d, sources[d])
            else:
                params = [p for p in program.parameters if d in p.dims]
                if not params:
                    raise DataError(
                        f"dimension '{d}' has no source: no parameter carries it and "
                        f"no explicit index was provided under key '{d}'"
                    )
                # derived dims carry no declared order — sorted values are the
                # deterministic fallback (pass an explicit index to control it)
                select = ' UNION '.join(f'SELECT DISTINCT {d} AS val FROM p_{p.name}' for p in params)
                self._con.execute(
                    f'CREATE TABLE dim_{d} AS SELECT val, ROW_NUMBER() OVER (ORDER BY val) - 1 AS ord FROM ({select})'
                )
            self._dim_card[d] = self._scalar(f'SELECT count(*) FROM dim_{d}')

    def _create_explicit_dim_table(self, d: str, source: Any) -> None:
        import pandas as pd

        if isinstance(source, (str, Path)):
            self._con.execute(
                f'CREATE TABLE dim_{d} AS '
                f'SELECT val, ROW_NUMBER() OVER (ORDER BY pos) - 1 AS ord FROM ('
                f'SELECT {d} AS val, MIN(file_row_number) AS pos '
                f"FROM read_parquet('{source}', file_row_number=true) GROUP BY {d})"
            )
            return
        if not isinstance(source, pd.DataFrame) or d not in source.columns:
            raise DataError(
                f"explicit index for dimension '{d}' must be a DataFrame with a "
                f"'{d}' column or a parquet path (got {type(source).__name__})"
            )
        vals = pd.unique(source[d])  # first occurrence = positional order
        frame = pd.DataFrame({'val': vals, 'ord': range(len(vals))})
        self._con.register(f'dimsrc_{d}', frame)
        self._con.execute(f'CREATE TABLE dim_{d} AS SELECT val, ord FROM dimsrc_{d}')
        self._con.unregister(f'dimsrc_{d}')

    # ------------------------------------------------------------------
    # frames (masked coord products with partition-wise labels)
    # ------------------------------------------------------------------

    def _frame_sql(self, dims: tuple[str, ...], where: ir.Predicate | None) -> tuple[str, str, str]:
        """FROM/WHERE clauses of the (masked) coord product and its order key.

        Returns ``(from_clause, where_clause, order_key)``; the select list can
        project ``t_<dim>.val AS <dim>``.
        """
        assert self._program is not None
        froms = [f'dim_{dims[0]} t_{dims[0]}']
        froms += [f'CROSS JOIN dim_{d} t_{d}' for d in dims[1:]]

        conds: list[str] = []
        if where is not None:
            joins, cond = self._pred_sql(where, dims)
            froms += joins
            conds.append(cond)
        from_clause = ' '.join(froms)
        where_clause = ' AND '.join(conds) if conds else 'TRUE'
        order_key = ', '.join(f't_{d}.ord' for d in dims)
        return from_clause, where_clause, order_key

    def _pred_sql(self, pred: ir.Predicate, dims: tuple[str, ...]) -> tuple[list[str], str]:
        assert self._program is not None
        joins: dict[str, str] = {}

        def join_param(param: str) -> str:
            """LEFT JOIN where-parameter *param* onto the frame; return its alias.

            Every parameter predicate needs this same join and the same
            containment check: a where-parameter carrying a dim the frame does
            not have would silently reduce the mask over that dim.
            """
            decl = self._program.parameter(param)
            extra = set(decl.dims) - set(dims)
            if extra:
                raise LanguageError(
                    f"where-parameter '{param}' has dims {sorted(extra)} outside the foreach dims {list(dims)}"
                )
            alias = f'w_{param}'
            on = ' AND '.join(f'{alias}.{d} = t_{d}.val' for d in decl.dims) or 'TRUE'
            joins[alias] = f'LEFT JOIN p_{param} {alias} ON {on}'
            return alias

        def walk(p: ir.Predicate) -> str:
            if isinstance(p, ir.ParameterComparison):
                alias = join_param(p.parameter)
                val = f"'{p.value}'" if isinstance(p.value, str) else repr(p.value)
                op = '=' if p.op == '==' else p.op
                return f'({alias}.value {op} {val})'
            if isinstance(p, ir.DimensionComparison):
                if p.dimension not in dims:
                    raise LanguageError(
                        f"where-comparison on dimension '{p.dimension}' is outside the foreach dims "
                        f'{list(dims)} — reducing a mask over an unlisted dim is not supported'
                    )
                val = f"'{p.value}'" if isinstance(p.value, str) else repr(p.value)
                op = '=' if p.op == '==' else p.op
                return f'(t_{p.dimension}.val {op} {val})'
            if isinstance(p, ir.ParameterDefined):
                alias = join_param(p.parameter)
                return f'({alias}.value IS NOT NULL AND isfinite({alias}.value))'
            if isinstance(p, ir.BooleanConstant):
                return 'TRUE' if p.value else 'FALSE'
            if isinstance(p, ir.And):
                return f'({walk(p.left)} AND {walk(p.right)})'
            if isinstance(p, ir.Or):
                return f'({walk(p.left)} OR {walk(p.right)})'
            if isinstance(p, ir.Not):
                return f'(NOT COALESCE({walk(p.operand)}, FALSE))'
            raise LanguageError(f'unsupported predicate node {type(p).__name__}')

        cond = walk(pred)
        # NULL comparisons (missing parameter rows) must exclude the row, not
        # yield NULL — wrap so the frame filter is strictly boolean.
        return list(joins.values()), f'COALESCE({cond}, FALSE)'

    def _chunk_starts(self, lead_dim: str, other_card: float) -> list[tuple[int, int]]:
        card = self._dim_card[lead_dim]
        per_chunk = max(1, int(self.chunk_rows // max(1.0, other_card)))
        return [(s, min(s + per_chunk, card)) for s in range(0, card, per_chunk)]

    def _build_variable(self, v: ir.VariableDeclaration) -> None:
        if not v.dims:
            raise LanguageError(f"variable '{v.name}' has no dims (scalars: use dims of size 1)")
        collist = ', '.join(f't_{d}.val AS {d}' for d in v.dims)
        from_clause, where_clause, order_key = self._frame_sql(v.dims, v.where)
        self._con.execute(
            f'CREATE TABLE var_{v.name} AS SELECT {collist}, 0::BIGINT AS var_label FROM {from_clause} WHERE FALSE'
        )

        other = math.prod(self._dim_card[d] for d in v.dims[1:]) if len(v.dims) > 1 else 1
        lead = v.dims[0]
        for lo, hi in self._chunk_starts(lead, other):
            self._n_cols += self._scalar(
                f"""
                INSERT INTO var_{v.name}
                SELECT {collist},
                       {self._n_cols} + ROW_NUMBER() OVER (ORDER BY {order_key}) - 1
                FROM {from_clause}
                WHERE t_{lead}.ord >= {lo} AND t_{lead}.ord < {hi} AND {where_clause}
                """
            )

        lb_sql, lb_joins = self._bound_sql(v.lower, v)
        ub_sql, ub_joins = self._bound_sql(v.upper, v)
        joins = ' '.join(dict.fromkeys(lb_joins + ub_joins))
        self._con.execute(
            f"INSERT INTO cols SELECT f.var_label, {lb_sql}, {ub_sql}, '{v.variable_type}' FROM var_{v.name} f {joins}"
        )
        bad = self._scalar('SELECT count(*) FROM cols WHERE lb IS NULL OR ub IS NULL')
        if bad:
            raise DataError(
                f"variable '{v.name}': {bad} rows have NULL bounds — a bound parameter "
                f'is missing values for some coordinates'
            )

    def _bound_sql(self, expr: ir.Expression, v: ir.VariableDeclaration) -> tuple[str, list[str]]:
        """Compile a variable-free bound expression to a scalar SQL expression
        over alias ``f`` (the variable frame), returning (sql, join clauses)."""
        assert self._program is not None
        joins: dict[str, str] = {}

        def walk(e: ir.Expression) -> str:
            if isinstance(e, ir.Constant):
                return _lit(e.value)
            if isinstance(e, ir.Parameter):
                decl = self._program.parameter(e.name)
                extra = set(decl.dims) - set(v.dims)
                if extra:
                    raise LanguageError(
                        f"bound parameter '{e.name}' of variable '{v.name}' has dims "
                        f'{sorted(extra)} outside the variable dims {list(v.dims)}'
                    )
                alias = f'b_{e.name}'
                on = ' AND '.join(f'{alias}.{d} = f.{d}' for d in decl.dims) or 'TRUE'
                joins[alias] = f'LEFT JOIN p_{e.name} {alias} ON {on}'
                return f'{alias}.value'
            if isinstance(e, ir.Negate):
                return f'(-({walk(e.operand)}))'
            if isinstance(e, ir.Add):
                return f'({walk(e.left)} + {walk(e.right)})'
            if isinstance(e, ir.Multiply):
                return f'({walk(e.left)} * {walk(e.right)})'
            raise LanguageError(
                f"unsupported node {type(e).__name__} in bounds of variable '{v.name}' "
                f'(bounds must be variable-free arithmetic over Constant/Parameter)'
            )

        return walk(expr), list(joins.values())

    # ------------------------------------------------------------------
    # expression compilation → pieces
    # ------------------------------------------------------------------

    def _compile(self, expr: ir.Expression, context: str) -> _CompiledExpression:
        assert self._program is not None
        prog = self._program

        def ev(e: ir.Expression) -> _CompiledExpression:
            if isinstance(e, ir.Constant):
                return _CompiledExpression((), (_TermFragment((), f'SELECT {_lit(e.value)} AS cval', False),))
            if isinstance(e, ir.Parameter):
                d = prog.parameter(e.name).dims
                cols = ', '.join([*d, 'value AS cval']) if d else 'value AS cval'
                return _CompiledExpression((), (_TermFragment(d, f'SELECT {cols} FROM p_{e.name}', False),))
            if isinstance(e, ir.Variable):
                d = prog.variable(e.name).dims
                cols = ', '.join([*d, 'var_label', '1.0 AS coeff'])
                return _CompiledExpression((_TermFragment(d, f'SELECT {cols} FROM var_{e.name}', True),), ())
            if isinstance(e, ir.Negate):
                inner = ev(e.operand)
                return _CompiledExpression(
                    tuple(_negate(p) for p in inner.terms),
                    tuple(_negate(p) for p in inner.consts),
                )
            if isinstance(e, ir.Add):
                a, b = ev(e.left), ev(e.right)
                return _CompiledExpression(a.terms + b.terms, a.consts + b.consts)
            if isinstance(e, ir.Multiply):
                a, b = ev(e.left), ev(e.right)
                if a.terms and b.terms:
                    raise LanguageError(f'nonlinear product in {context}: both factors contain variables')
                if b.terms:  # normalise: terms on the left
                    a, b = b, a
                terms = tuple(_join_mul(t, c, is_term=True) for t in a.terms for c in b.consts)
                consts = tuple(_join_mul(x, c, is_term=False) for x in a.consts for c in b.consts)
                return _CompiledExpression(terms, consts)
            if isinstance(e, ir.Divide):
                a, b = ev(e.numerator), ev(e.divisor)
                if b.terms:
                    raise LanguageError(f'nonlinear quotient in {context}: the divisor contains variables')
                if len(b.consts) != 1:
                    raise LanguageError(
                        f'in {context}: a divisor must be a single Constant/Parameter factor, '
                        f'not a sum — rewrite as multiplication by a precomputed parameter'
                    )
                inv = b.consts[0]
                terms = tuple(_join_mul(t, inv, is_term=True, op='/') for t in a.terms)
                consts = tuple(_join_mul(x, inv, is_term=False, op='/') for x in a.consts)
                return _CompiledExpression(terms, consts)
            if isinstance(e, ir.Sum):
                inner = ev(e.operand)
                terms = tuple(self._sum_fragment(p, e.over, context) for p in inner.terms)
                consts = tuple(self._sum_fragment(p, e.over, context) for p in inner.consts)
                return _CompiledExpression(terms, consts)
            if isinstance(e, ir.GroupSum):
                inner = ev(e.operand)
                terms = tuple(self._group_fragment(p, e, context) for p in inner.terms)
                consts = tuple(self._group_fragment(p, e, context) for p in inner.consts)
                return _CompiledExpression(terms, consts)
            if isinstance(e, ir.Translate):
                inner = ev(e.operand)
                terms = tuple(self._translate_fragment(p, e, context) for p in inner.terms)
                consts = tuple(self._translate_fragment(p, e, context) for p in inner.consts)
                return _CompiledExpression(terms, consts)
            raise LanguageError(f'unsupported expression node {type(e).__name__} in {context}')

        return ev(expr)

    def _sum_fragment(self, p: _TermFragment, over: tuple[str, ...], context: str) -> _TermFragment:
        missing = [d for d in over if d not in p.dims]
        if missing and not p.is_term:
            raise LanguageError(
                f'in {context}: Sum over {list(over)} of a constant part lacking dims '
                f'{missing} is ambiguous under masks — multiply explicitly instead'
            )
        keep = tuple(d for d in p.dims if d not in over)
        scale = math.prod(self._dim_card[d] for d in missing)
        valcols = 'var_label, coeff' if p.is_term else 'cval'
        if scale != 1:
            valcols = f'var_label, coeff * {scale} AS coeff' if p.is_term else f'cval * {scale} AS cval'
        cols = ', '.join([*keep, valcols]) if keep else valcols
        return _TermFragment(keep, f'SELECT {cols} FROM ({p.sql})', p.is_term)

    def _group_fragment(self, p: _TermFragment, g: ir.GroupSum, context: str) -> _TermFragment:
        assert self._program is not None
        mdecl = self._program.parameter(g.mapping)
        if len(mdecl.dims) != 1:
            raise LanguageError(
                f"in {context}: GroupSum mapping '{g.mapping}' must have exactly one dim (has {list(mdecl.dims)})"
            )
        d = mdecl.dims[0]
        if d not in p.dims:
            raise LanguageError(f"in {context}: GroupSum over '{d}' but the expression has dims {list(p.dims)}")
        keep = tuple(x for x in p.dims if x != d)
        valcols = 't.var_label, t.coeff' if p.is_term else 't.cval'
        keepcols = ', '.join([*(f't.{x}' for x in keep), f'm.value AS {g.into}', valcols])
        sql = f'SELECT {keepcols} FROM ({p.sql}) t JOIN p_{g.mapping} m ON m.{d} = t.{d}'
        return _TermFragment((*keep, g.into), sql, p.is_term)

    def _translate_fragment(self, p: _TermFragment, s: ir.Translate, context: str) -> _TermFragment:
        """Translation = a pointwise remap of the dim through its ord:
        a row at ord *o* contributes to the output coord at ord ``(o + by) %
        card``. No window function involved — bounded-halo locality."""
        if s.dimension not in p.dims:
            raise LanguageError(
                f"in {context}: translation along '{s.dimension}' but the expression has dims {list(p.dims)}"
            )
        card = self._dim_card[s.dimension]
        others = [d for d in p.dims if d != s.dimension]
        valcols = 't.var_label, t.coeff' if p.is_term else 't.cval'
        cols = ', '.join([*(f't.{d}' for d in others), f'd_out.val AS {s.dimension}', valcols])
        if s.wrap:
            on = f'd_out.ord = ((d_in.ord + {s.by}) % {card} + {card}) % {card}'
        else:
            # acyclic: out-of-range rows simply don't join — zero contribution
            on = f'd_out.ord = d_in.ord + {s.by}'
        sql = (
            f'SELECT {cols} FROM ({p.sql}) t '
            f'JOIN dim_{s.dimension} d_in ON d_in.val = t.{s.dimension} '
            f'JOIN dim_{s.dimension} d_out ON {on}'
        )
        return _TermFragment(p.dims, sql, p.is_term)

    # ------------------------------------------------------------------
    # constraints and objective
    # ------------------------------------------------------------------

    def _build_constraint(self, c: ir.ConstraintDeclaration) -> None:
        if not c.dims:
            raise LanguageError(f"constraint '{c.name}' has no dims")
        lhs = self._compile(c.lhs, f"constraint '{c.name}' lhs")
        rhs = self._compile(c.rhs, f"constraint '{c.name}' rhs")
        # normalise: terms on the left (rhs terms negated), consts on the right
        terms = [(p, 1.0) for p in lhs.terms] + [(p, -1.0) for p in rhs.terms]
        consts = [(p, 1.0) for p in rhs.consts] + [(p, -1.0) for p in lhs.consts]
        for p, _ in [*terms, *consts]:
            extra = set(p.dims) - set(c.dims)
            if extra:
                raise LanguageError(
                    f"constraint '{c.name}': expression has dims {sorted(extra)} outside "
                    f'foreach {list(c.dims)} — missing a Sum/GroupSum?'
                )

        collist = ', '.join(f't_{d}.val AS {d}' for d in c.dims)
        from_clause, where_clause, order_key = self._frame_sql(c.dims, c.where)
        self._con.execute(
            f'CREATE TABLE con_{c.name} AS SELECT {collist}, 0::BIGINT AS row FROM {from_clause} WHERE FALSE'
        )

        # label assignment is the one step that needs the partition loop
        # (a global ROW_NUMBER window materialises its whole input)
        other = math.prod(self._dim_card[d] for d in c.dims[1:]) if len(c.dims) > 1 else 1
        lead = c.dims[0]
        for lo, hi in self._chunk_starts(lead, other):
            self._n_rows += self._scalar(
                f"""
                INSERT INTO con_{c.name}
                SELECT {collist},
                       {self._n_rows} + ROW_NUMBER() OVER (ORDER BY {order_key}) - 1
                FROM {from_clause}
                WHERE t_{lead}.ord >= {lo} AND t_{lead}.ord < {hi} AND {where_clause}
                """
            )

        rhs_sql = ' + '.join(f'{sign} * COALESCE(({self._agg_const_join(p, c.dims)}), 0)' for p, sign in consts) or '0'

        term_selects = []
        for p, sign in terms:
            on = ' AND '.join(f'f.{d} = t.{d}' for d in p.dims) or 'TRUE'
            term_selects.append(
                f'SELECT f.row, t.var_label AS col, {sign} * t.coeff AS coeff FROM f JOIN ({p.sql}) t ON {on}'
            )
        union = ' UNION ALL '.join(term_selects)

        # single-shot assembly: joins and the plain numeric hash aggregate
        # spill under memory_limit on their own — no chunking needed (only
        # ordered/string aggregates and global windows can't spill)
        frame = f'SELECT * FROM con_{c.name}'
        if term_selects:
            self._con.execute(
                f'INSERT INTO A WITH f AS ({frame}) SELECT row, col, SUM(coeff) FROM ({union}) GROUP BY row, col'
            )
        self._con.execute(f"INSERT INTO rows WITH f AS ({frame}) SELECT f.row, '{c.sense}', {rhs_sql} FROM f")

    def _agg_const_join(self, p: _TermFragment, frame_dims: tuple[str, ...]) -> str:
        """Correlated scalar: the summed const piece value for frame row ``f``."""
        cond = ' AND '.join(f'q.{d} = f.{d}' for d in p.dims) or 'TRUE'
        return f'SELECT SUM(q.cval) FROM ({p.sql}) q WHERE {cond}'

    def _build_objective(self, o: ir.ObjectiveDeclaration) -> None:
        comp = self._compile(o.expression, 'objective')
        for p in comp.consts:
            if p.dims:
                raise LanguageError(
                    'objective constant part has dims — wrap parameter terms in '
                    'Mul with a Var, or pre-aggregate to a scalar'
                )
            self._obj_const += self._scalar(p.sql) or 0.0
        if comp.terms:
            union = ' UNION ALL '.join(f'SELECT var_label AS col, coeff FROM ({p.sql})' for p in comp.terms)
            self._con.execute(f'INSERT INTO obj SELECT col, SUM(coeff) FROM ({union}) GROUP BY col')
        self._obj_sense = o.sense

    # ------------------------------------------------------------------
    # sink: LP file
    # ------------------------------------------------------------------

    def write_lp(self, path: str | Path) -> None:
        path = Path(path)
        parts = self.workdir / 'lp_parts'
        parts.mkdir(exist_ok=True)

        self._con.execute(f"COPY (SELECT printf('%+.17g x%d', coeff, col) FROM obj) TO '{parts / 'obj'}' {_COPY_OPTS}")

        nnz = self._scalar('SELECT count(*) FROM A')
        avg = max(1, nnz // max(1, self._n_rows))
        per_chunk = max(1, self.chunk_rows // avg)
        con_parts = []
        for i, lo in enumerate(range(0, max(self._n_rows, 1), per_chunk)):
            hi = min(lo + per_chunk, self._n_rows)
            if hi <= lo:
                break
            part = parts / f'cons.{i}'
            con_parts.append(part)
            self._con.execute(
                f"""
                COPY (
                    SELECT printf('c%d:', r.row) || chr(10)
                           || COALESCE(string_agg(printf('%+.17g x%d', a.coeff, a.col), chr(10)), '+0 x0')
                           || chr(10)
                           || printf('%s %.17g', CASE r.sense WHEN '==' THEN '=' ELSE r.sense END, r.rhs)
                    FROM rows r LEFT JOIN A a USING (row)
                    WHERE r.row >= {lo} AND r.row < {hi}
                    GROUP BY r.row, r.sense, r.rhs
                ) TO '{part}' {_COPY_OPTS}
                """
            )

        self._con.execute(
            f"""
            COPY (
                SELECT CASE WHEN lb = '-infinity'::DOUBLE THEN '-infinity' ELSE printf('%.17g', lb) END
                       || printf(' <= x%d <= ', col)
                       || CASE WHEN ub = 'infinity'::DOUBLE THEN '+infinity' ELSE printf('%.17g', ub) END
                FROM cols
            ) TO '{parts / 'bounds'}' {_COPY_OPTS}
            """
        )

        integrality_sections = []
        for vtype, keyword in (('binary', 'binary'), ('integer', 'general')):
            n = self._scalar(f"SELECT count(*) FROM cols WHERE vtype = '{vtype}'")
            if n:
                part = parts / keyword
                integrality_sections.append((keyword, part))
                self._con.execute(
                    f"COPY (SELECT printf('x%d', col) FROM cols WHERE vtype = '{vtype}') TO '{part}' {_COPY_OPTS}"
                )

        sense = b'min' if self._obj_sense == 'min' else b'max'
        with open(path, 'wb') as f:
            f.write(sense + b'\n\nobj:\n')
            if self._obj_const:
                f.write(f'{self._obj_const:+.17g}\n'.encode())
            _cat(f, parts / 'obj')
            f.write(b'\ns.t.\n\n')
            for part in con_parts:
                _cat(f, part)
            f.write(b'\nbounds\n')
            _cat(f, parts / 'bounds')
            for keyword, part in integrality_sections:
                f.write(f'\n{keyword}\n'.encode())
                _cat(f, part)
            f.write(b'\nend\n')
        shutil.rmtree(parts)

    # ------------------------------------------------------------------
    # sink: solver_direct (HiGHS)
    # ------------------------------------------------------------------

    def solve(self, batch_rows: int = 100_000) -> Solution:
        import highspy
        import numpy as np

        inf = highspy.kHighsInf
        h = highspy.Highs()
        h.setOptionValue('output_flag', False)

        empty_i = np.empty(0, dtype=np.int32)
        empty_f = np.empty(0, dtype=np.float64)
        reader = self._con.execute(
            'SELECT c.col, c.lb, c.ub, c.vtype, COALESCE(o.coeff, 0) AS cost '
            'FROM cols c LEFT JOIN obj o USING (col) ORDER BY c.col'
        ).to_arrow_reader(batch_rows)
        for batch in reader:
            d = batch.to_pydict()
            lb = np.nan_to_num(np.asarray(d['lb'], dtype=np.float64), neginf=-inf, posinf=inf)
            ub = np.nan_to_num(np.asarray(d['ub'], dtype=np.float64), neginf=-inf, posinf=inf)
            cost = np.asarray(d['cost'], dtype=np.float64)
            h.addCols(len(cost), cost, lb, ub, 0, empty_i, empty_i, empty_f)
            vtype = np.asarray(d['vtype'])
            noncont = np.flatnonzero(vtype != 'continuous')
            if len(noncont):
                cols_idx = np.asarray(d['col'], dtype=np.int32)[noncont]
                integrality = np.full(len(noncont), int(highspy.HighsVarType.kInteger), dtype=np.uint8)
                h.changeColsIntegrality(len(noncont), cols_idx, integrality)

        for lo in range(0, max(self._n_rows, 1), batch_rows):
            hi = min(lo + batch_rows, self._n_rows)
            if hi <= lo:
                break
            rows = self._con.execute(
                f'SELECT row, sense, rhs FROM rows WHERE row >= {lo} AND row < {hi} ORDER BY row'
            ).fetchnumpy()
            a = self._con.execute(
                f'SELECT row, col, coeff FROM A WHERE row >= {lo} AND row < {hi} ORDER BY row'
            ).fetchnumpy()
            rhs = np.asarray(rows['rhs'], dtype=np.float64)
            sense = rows['sense']
            rlb = np.where(sense == '<=', -inf, rhs)
            rub = np.where(sense == '>=', inf, rhs)
            starts = np.searchsorted(np.asarray(a['row']), np.asarray(rows['row'])).astype(np.int32)
            h.addRows(
                len(rhs),
                rlb,
                rub,
                len(a['col']),
                starts,
                np.asarray(a['col'], dtype=np.int32),
                np.asarray(a['coeff'], dtype=np.float64),
            )

        if self._obj_sense == 'max':
            h.changeObjectiveSense(highspy.ObjSense.kMaximize)
        h.run()

        status = str(h.getModelStatus()).rsplit('.', 1)[-1].removeprefix('k')
        objective = h.getInfo().objective_function_value + self._obj_const

        import pandas as pd

        sol = pd.DataFrame(
            {
                'col': np.arange(self._n_cols, dtype=np.int64),
                'value': np.asarray(h.getSolution().col_value, dtype=np.float64),
            }
        )
        self._con.execute('DROP TABLE IF EXISTS sol')
        self._con.register('sol_src', sol)
        self._con.execute('CREATE TABLE sol AS SELECT * FROM sol_src')
        self._con.unregister('sol_src')
        return Solution(status=status, objective=objective, _executor=self)

    def _primal(self, name: str) -> pd.DataFrame:
        assert self._program is not None
        dims = self._program.variable(name).dims
        collist = ', '.join([*(f'v.{d}' for d in dims), 's.value'])
        return self._con.execute(f'SELECT {collist} FROM var_{name} v JOIN sol s ON s.col = v.var_label').df()

    def _solution_to_parquet(self, directory: Path) -> dict[str, Path]:
        assert self._program is not None
        directory.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}
        for v in self._program.variables:
            out = directory / f'{v.name}.parquet'
            collist = ', '.join([*(f'v.{d}' for d in v.dims), 's.value'])
            self._con.execute(
                f'COPY (SELECT {collist} FROM var_{v.name} v JOIN sol s ON s.col = v.var_label) '
                f"TO '{out}' (FORMAT parquet)"
            )
            written[v.name] = out
        return written

    # ------------------------------------------------------------------

    def close(self) -> None:
        self._finalizer()

    def __enter__(self) -> DuckdbExecutor:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        self.close()
        return False


def _release(con: Any, workdir: Path | None) -> None:
    with contextlib.suppress(Exception):  # best-effort at interpreter exit
        con.close()
    if workdir is not None:
        shutil.rmtree(workdir, ignore_errors=True)


def _lit(v: float) -> str:
    if math.isinf(v):
        return "('infinity'::DOUBLE)" if v > 0 else "('-infinity'::DOUBLE)"
    # _lit(0) type-checks (int -> float) and would emit '0': INTEGER, not DOUBLE
    # pyrefly: ignore[unnecessary-type-conversion]
    return repr(float(v))


def _negate(p: _TermFragment) -> _TermFragment:
    cols = 'var_label, -coeff AS coeff' if p.is_term else '-cval AS cval'
    sel = ', '.join([*p.dims, cols]) if p.dims else cols
    return _TermFragment(p.dims, f'SELECT {sel} FROM ({p.sql})', p.is_term)


def _join_mul(a: _TermFragment, c: _TermFragment, is_term: bool, op: str = '*') -> _TermFragment:
    """a op c where ``c`` is a const piece; join on shared dims, broadcast the rest."""
    shared = [d for d in a.dims if d in c.dims]
    on = ' AND '.join(f'a.{d} = c.{d}' for d in shared) or 'TRUE'
    out_dims = a.dims + tuple(d for d in c.dims if d not in a.dims)
    dimcols = [
        *(f'a.{d}' for d in a.dims),
        *(f'c.{d}' for d in c.dims if d not in a.dims),
    ]
    val = f'a.var_label, a.coeff {op} c.cval AS coeff' if is_term else f'a.cval {op} c.cval AS cval'
    sel = ', '.join([*dimcols, val])
    return _TermFragment(
        out_dims,
        f'SELECT {sel} FROM ({a.sql}) a JOIN ({c.sql}) c ON {on}',
        is_term,
    )


def _cat(f: Any, part: Path) -> None:
    with open(part, 'rb') as src:
        shutil.copyfileobj(src, f)
