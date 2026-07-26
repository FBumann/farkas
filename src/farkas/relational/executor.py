"""Duckdb executor: fill the model tables, then hand them to a sink.

The lane is described in ARCHITECTURE.md, "The relational lane".

This module owns the *connection* and the tables in it. It does not own the
SQL — :mod:`farkas.relational.compiler` turns plan nodes into strings and
never touches a connection — and it does not own the way a model leaves:
:mod:`farkas.relational.sinks` drains the tables into LP text or into
HiGHS, one module per sink.

What is left here is what genuinely needs the database: binding sources,
building the dim tables, assigning labels, assembling ``cols``/``obj``/
``rows``/``A``, and joining solution values back to coordinates.

Hand-managed chunking exists in exactly two places, both forced by operators
duckdb cannot spill:

1. Label assignment (here) — a global ``ROW_NUMBER`` window materialises its
   whole input, so labels are assigned per-chunk of the leading dim with a
   running offset. This is one generic mechanism; every operator inherits it.
2. LP-text ``string_agg`` (in the sink) — string aggregates don't spill, and a
   fixed conservative chunk size costs nothing in the debugging sink.

Everything else — joins, scaling, masks, and the numeric hash aggregates that
assemble ``A`` — delegates to duckdb's own spilling under ``memory_limit``.
Future plan operators should be classified by coordinate locality: pointwise
(joins/masks/group_sum) and bounded-halo (roll: t±k, which still works under
label chunking because terms join the *global* variable table) compose freely;
genuinely global operators (running sums, normalisations) must be rejected at
lowering with a rewrite hint (e.g. running sum -> state-variable recurrence).

duckdb, pyarrow and numpy are imported lazily. Arrow is the only in-memory
table this module knows: sources arrive as ``pyarrow.Table`` (or a parquet
path) and results leave as ``pyarrow.Table``, so no dataframe library is a
dependency of the lane. ``sources.tidy_sources`` is where a caller's
pandas/polars/xarray object is turned into one.
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

from farkas.errors import DataError, LanguageError, LinopyYamlError
from farkas.relational import plan, sinks
from farkas.relational.arrow import as_table
from farkas.relational.compiler import SqlCompiler

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pandas as pd

_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

#: Column an index table carries its row position in. The space makes it
#: unrepresentable as a declared name (``_IDENT``), so it cannot collide with a
#: dimension or coordinate the caller's table already has.
_ROW_POSITION = '__row position__'


#: Deprecated. The engine's failures are now split between
#: :class:`~farkas.errors.LanguageError` (the program says something the
#: engine cannot build) and :class:`~farkas.errors.DataError` (a source is
#: missing or the wrong shape). This alias is their common base, so an existing
#: ``except RelationalBuildError`` keeps catching everything it used to.
RelationalBuildError = LinopyYamlError


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
        installed (it ships with the ``[linopy]`` extra); missing coordinate
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
    """Build a :class:`Program` into tables under a memory budget, then sink it."""

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

        self._program: plan.Program | None = None
        self._compiler: SqlCompiler | None = None
        self._bool_params: set[str] = set()
        self._dim_card: dict[str, int] = {}
        self._n_cols = 0
        self._n_rows = 0
        self._obj_const = 0.0
        self._obj_sense: str = 'min'

    # ------------------------------------------------------------------
    # build
    # ------------------------------------------------------------------

    def build(self, program: plan.Program, sources: Mapping[str, Any]) -> None:
        """Load sources, create dim/parameter tables, variables, constraints."""
        self._program = program
        self._validate_names(program)

        for p in program.parameters:
            self._create_param_table(p, sources)
        self._create_dim_tables(program, sources)

        # the compiler is built after the data, because two of its answers
        # depend on it: sum over an absent dim scales by that dim's size, and
        # `defined` on a boolean parameter tests the value, not its finiteness
        self._compiler = SqlCompiler(program, dict(self._dim_card), frozenset(self._bool_params))

        self._con.execute('CREATE TABLE cols (col BIGINT, lb DOUBLE, ub DOUBLE, vtype VARCHAR)')
        self._con.execute('CREATE TABLE obj (col BIGINT, coeff DOUBLE)')
        self._con.execute('CREATE TABLE rows (row BIGINT, sense VARCHAR, rhs DOUBLE)')
        self._con.execute('CREATE TABLE A (row BIGINT, col BIGINT, coeff DOUBLE)')

        for v in program.variables:
            self._build_variable(v)
        for c in program.constraints:
            self._build_constraint(c)
        self._build_objective(program.objective)

    def _validate_names(self, program: plan.Program) -> None:
        names = (
            [p.name for p in program.parameters]
            + [v.name for v in program.variables]
            + [c.name for c in program.constraints]
            + [d for p in program.parameters for d in p.dims]
            + [d for v in program.variables for d in v.dims]
            + [d.name for d in program.dimensions]
            + [c for d in program.dimensions for pair in d.coordinates for c in pair]
        )
        for n in names:
            if not _IDENT.match(n):
                raise LanguageError(f"name '{n}' is not a valid identifier ([A-Za-z_][A-Za-z0-9_]*)")

    @property
    def _sql(self) -> SqlCompiler:
        assert self._compiler is not None, 'build() has not run'
        return self._compiler

    def _create_param_table(self, p: plan.ParameterDeclaration, sources: Mapping[str, Any]) -> None:
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
        if self._value_type(f'p_{p.name}') == 'BOOLEAN':
            self._bool_params.add(p.name)

    def _value_type(self, table: str) -> str:
        rows = self._con.execute(f"SELECT column_type FROM (DESCRIBE {table}) WHERE column_name = 'value'").fetchall()
        return str(rows[0][0]) if rows else ''

    def _source_relation(self, name: str, source: Any) -> str:
        if isinstance(source, (str, Path)):
            return f"read_parquet('{source}')"
        table = as_table(source)
        if table is not None:
            self._con.register(f'src_{name}', table)
            return f'src_{name}'
        raise DataError(
            f"source for '{name}' must be a parquet path or an Arrow-compatible "
            f'table — pyarrow, polars, pandas (got {type(source).__name__})'
        )

    def _relation_columns(self, rel: str) -> list[str]:
        return [d[0] for d in self._con.execute(f'SELECT * FROM {rel} LIMIT 0').description]

    def _scalar(self, sql: str):
        row = self._con.execute(sql).fetchone()
        assert row is not None
        return row[0]

    def _create_dim_tables(self, program: plan.Program, sources: Mapping[str, Any]) -> None:
        assert self._program is not None
        dims: set[str] = set()
        for v in program.variables:
            dims.update(v.dims)
        for c in program.constraints:
            dims.update(c.dims)
        for p in program.parameters:
            dims.update(p.dims)

        for d in sorted(dims):
            coordinates = dict(program.dimension(d).coordinates)
            if d in sources:
                # explicit index: ordinals follow declared/coords order, so
                # Translate's positional semantics match xarray/linopy exactly
                # even for non-monotonic or string coordinates
                self._create_explicit_dim_table(d, sources[d], coordinates)
            else:
                if coordinates:
                    raise DataError(
                        f"dimension '{d}' declares coordinates {sorted(coordinates)} but has "
                        f"no index source. Pass one under key '{d}' (a parquet path or frame "
                        f'carrying columns {[d, *sorted(coordinates)]}) — a coordinate cannot '
                        f'be inferred from the parameters that happen to use the dimension.'
                    )
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

        # Coordinate containment, once every dim table exists: a coordinate's
        # values must be labels of the dimension it targets. This is the check
        # that stops a mistyped label from vanishing in the inner join that
        # places its terms — it built and solved, with the term silently gone.
        for d in sorted(dims):
            for cname, target in sorted(program.dimension(d).coordinates):
                if target not in self._dim_card:
                    raise DataError(
                        f"dimension '{d}' coordinate '{cname}' targets '{target}', which "
                        f'no declaration in this model uses, so it has no coordinate set '
                        f'to check against'
                    )
                self._check_coordinate_containment(d, cname, target)

    def _create_explicit_dim_table(self, d: str, source: Any, coordinates: Mapping[str, str]) -> None:
        import pyarrow as pa

        # coordinate names are language identifiers, and SQL reserves some of
        # them ("from" and "to" are the natural names for a line's endpoints),
        # so every reference to one is quoted
        names = sorted(coordinates)
        agg = ''.join(f', ANY_VALUE("{c}") AS "{c}"' for c in names)
        outer = ''.join(f', "{c}"' for c in names)

        if isinstance(source, (str, Path)):
            self._con.execute(
                f'CREATE TABLE dim_{d} AS '
                f'SELECT val, ROW_NUMBER() OVER (ORDER BY pos) - 1 AS ord{outer} FROM ('
                f'SELECT {d} AS val, MIN(file_row_number) AS pos{agg} '
                f"FROM read_parquet('{source}', file_row_number=true) GROUP BY {d})"
            )
            self._check_coordinates_single_valued(d, names, f"read_parquet('{source}')")
            return
        source = as_table(source)
        if source is None or d not in source.column_names:
            raise DataError(
                f"explicit index for dimension '{d}' must be an Arrow-compatible "
                f"table with a '{d}' column, or a parquet path"
            )
        missing = [c for c in names if c not in source.column_names]
        if missing:
            raise DataError(
                f"index for dimension '{d}' is missing declared coordinate column(s) "
                f'{missing} (has {source.column_names})'
            )
        # first occurrence of a label is its position. Row order is data, not a
        # property duckdb preserves (preserve_insertion_order is off), so it is
        # carried explicitly — the in-memory twin of the parquet branch's
        # file_row_number.
        index = source.select([d, *names])
        index = index.append_column(_ROW_POSITION, pa.array(range(index.num_rows), type=pa.int64()))
        self._con.register(f'dimsrc_{d}', index)
        self._con.execute(
            f'CREATE TABLE dim_{d} AS '
            f'SELECT val, ROW_NUMBER() OVER (ORDER BY pos) - 1 AS ord{outer} FROM ('
            f'SELECT {d} AS val, MIN("{_ROW_POSITION}") AS pos{agg} FROM dimsrc_{d} GROUP BY {d})'
        )
        self._check_coordinates_single_valued(d, names, f'dimsrc_{d}')
        self._con.unregister(f'dimsrc_{d}')

    def _check_coordinates_single_valued(self, d: str, names: list[str], relation: str) -> None:
        """One label, one coordinate value — two rows disagreeing is a data bug."""
        for c in names:
            bad = self._scalar(
                f'SELECT count(*) FROM (SELECT {d} FROM {relation} GROUP BY {d} HAVING count(DISTINCT "{c}") > 1)'
            )
            if bad:
                raise DataError(
                    f"dimension '{d}': {bad} label(s) carry more than one value for "
                    f"coordinate '{c}'. A coordinate is single-valued per label — "
                    f'reduce the source to one row per {d}, or model the relation as a '
                    f'parameter instead.'
                )

    def _check_coordinate_containment(self, d: str, cname: str, target: str) -> None:
        """Every coordinate value must be a label of the dimension it targets.

        A *null* value is not a violation: it says the label belongs to no
        group, which is the same row-absence idiom the rest of the engine uses
        for "not present". Only a value that is present and unknown is a typo,
        and that is the case worth stopping — it would drop terms silently.
        """
        bad = self._con.execute(
            f'SELECT g."{cname}" FROM dim_{d} g LEFT JOIN dim_{target} t ON t.val = g."{cname}" '
            f'WHERE t.val IS NULL AND g."{cname}" IS NOT NULL GROUP BY g."{cname}" LIMIT 5'
        ).fetchall()
        if not bad:
            return
        shown = ', '.join(repr(r[0]) for r in bad)
        raise DataError(
            f"dimension '{d}' coordinate '{cname}' has value(s) that are not "
            f"'{target}' coordinates: {shown}. Every value must be a declared "
            f"'{target}' label — otherwise group_sum(over={d}, by={cname}) drops "
            f'those terms in the join that places them, and the model builds and '
            f'solves without them.'
        )

    def _chunk_starts(self, lead_dim: str, other_card: float) -> list[tuple[int, int]]:
        card = self._dim_card[lead_dim]
        per_chunk = max(1, int(self.chunk_rows // max(1.0, other_card)))
        return [(s, min(s + per_chunk, card)) for s in range(0, card, per_chunk)]

    def _predicate_dims(self, where: plan.Predicate | None) -> frozenset[str]:
        """The dims a mask reads. Empty means it filters nothing per-coordinate."""
        if where is None:
            return frozenset()
        assert self._program is not None, 'build() has not run'
        param_dims = {p.name: frozenset(p.dims) for p in self._program.parameters}
        match where:
            case plan.BooleanConstant():
                return frozenset()
            case plan.DimensionComparison(dimension=d):
                return frozenset({d})
            case plan.ParameterComparison(parameter=n) | plan.ParameterDefined(parameter=n):
                return param_dims.get(n, frozenset())
            case plan.Not(operand=x):
                return self._predicate_dims(x)
            case plan.And(left=a, right=b) | plan.Or(left=a, right=b):
                return self._predicate_dims(a) | self._predicate_dims(b)
        raise LanguageError(f'unhandled predicate {type(where).__name__}')

    def _label_frame(
        self,
        table: str,
        dims: tuple[str, ...],
        where: plan.Predicate | None,
        label: str,
        start: int,
    ) -> int:
        """Materialise the masked coord product of *dims* as *table*, with a
        dense ``label`` column continuing from *start*. Returns the next label.

        Variables (``var_label``) and constraint rows (``row``) are the same
        operation over different tables, and it is the one place chunking is
        hand-managed: a global ``ROW_NUMBER`` window materialises its whole
        input, so labels are assigned per-chunk of the leading dim with a
        running offset. Writing that twice is how the two would come to
        disagree about which coordinate gets which solver index.
        """
        collist = ', '.join(f't_{d}.val AS {d}' for d in dims)
        from_clause, where_clause, order_key = self._sql.frame(dims, where)
        self._con.execute(
            f'CREATE TABLE {table} AS SELECT {collist}, 0::BIGINT AS {label} FROM {from_clause} WHERE FALSE'
        )

        lead, *rest = dims
        other = math.prod(self._dim_card[d] for d in rest)

        # When the mask does not read the leading dim, the surviving trailing
        # coordinates are identical for every leading value. Rank them once
        # over a table of size `other` and the label is arithmetic: no window
        # function, no per-chunk sort. Same labels as the general path below.
        if rest and lead not in self._predicate_dims(where):
            surv_from, surv_where, surv_order = self._sql.frame(tuple(rest), where)
            surv_cols = ', '.join(f't_{d}.val AS {d}' for d in rest)
            self._con.execute(f'DROP TABLE IF EXISTS surv_{table}')
            self._con.execute(
                f'CREATE TABLE surv_{table} AS SELECT {surv_cols}, '
                f'ROW_NUMBER() OVER (ORDER BY {surv_order}) - 1 AS _rank '
                f'FROM {surv_from} WHERE {surv_where}'
            )
            width = self._scalar(f'SELECT count(*) FROM surv_{table}')
            picks = ', '.join(f's.{d}' for d in rest)
            next_label = start
            for lo, hi in self._chunk_starts(lead, other):
                next_label += self._scalar(
                    f'INSERT INTO {table} SELECT t_{lead}.val AS {lead}, {picks}, '
                    f'{next_label} + (t_{lead}.ord - {lo}) * {width} + s._rank '
                    f'FROM dim_{lead} t_{lead} CROSS JOIN surv_{table} s '
                    f'WHERE t_{lead}.ord >= {lo} AND t_{lead}.ord < {hi}'
                )
            self._con.execute(f'DROP TABLE surv_{table}')
            return next_label

        next_label = start
        for lo, hi in self._chunk_starts(lead, other):
            next_label += self._scalar(
                f"""
                INSERT INTO {table}
                SELECT {collist},
                       {next_label} + ROW_NUMBER() OVER (ORDER BY {order_key}) - 1
                FROM {from_clause}
                WHERE t_{lead}.ord >= {lo} AND t_{lead}.ord < {hi} AND {where_clause}
                """
            )
        return next_label

    def _build_variable(self, v: plan.VariableDeclaration) -> None:
        if not v.dims:
            raise LanguageError(f"variable '{v.name}' has no dims (scalars: use dims of size 1)")
        self._n_cols = self._label_frame(f'var_{v.name}', v.dims, v.where, 'var_label', self._n_cols)

        lb_sql, lb_joins = self._sql.bound(v.lower, v)
        ub_sql, ub_joins = self._sql.bound(v.upper, v)
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

    def _build_constraint(self, c: plan.ConstraintDeclaration) -> None:
        if not c.dims:
            raise LanguageError(f"constraint '{c.name}' has no dims")
        lhs = self._sql.expression(c.lhs, f"constraint '{c.name}' lhs")
        rhs = self._sql.expression(c.rhs, f"constraint '{c.name}' rhs")
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

        self._n_rows = self._label_frame(f'con_{c.name}', c.dims, c.where, 'row', self._n_rows)

        rhs_sql = ' + '.join(f'{sign} * COALESCE(({self._sql.constant_scalar(p)}), 0)' for p, sign in consts) or '0'

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

    def _build_objective(self, o: plan.ObjectiveDeclaration) -> None:
        comp = self._sql.expression(o.expression, 'objective')
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
    # sinks — see relational/sinks/; the executor only supplies the tables
    # ------------------------------------------------------------------

    def _tables(self) -> sinks.ModelTables:
        return sinks.ModelTables(
            connection=self._con,
            workdir=self.workdir,
            chunk_rows=self.chunk_rows,
            column_count=self._n_cols,
            row_count=self._n_rows,
            objective_sense=self._obj_sense,
            objective_constant=self._obj_const,
        )

    def write_lp(self, path: str | Path) -> None:
        """Sink the built model to an LP file."""
        sinks.write_lp_file(self._tables(), path)

    def solve(self, batch_rows: int = 100_000) -> Solution:
        """Sink the built model straight into HiGHS and solve it."""
        status, objective = sinks.solve_direct(self._tables(), batch_rows)
        return Solution(status=status, objective=objective, _executor=self)

    def _solution_sql(self, name: str) -> str:
        """The tidy solution of variable *name*: ``(dims…, value)``.

        A label join, never a dense array — which is what makes reading results
        back cost the same whichever sink asked for them.
        """
        assert self._program is not None
        collist = ', '.join([*(f'v.{d}' for d in self._program.variable(name).dims), 's.value'])
        return f'SELECT {collist} FROM var_{name} v JOIN sol s ON s.col = v.var_label'

    def _primal(self, name: str) -> pd.DataFrame:
        return self._con.execute(self._solution_sql(name)).df()

    def _solution_to_parquet(self, directory: Path) -> dict[str, Path]:
        assert self._program is not None
        directory.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}
        for v in self._program.variables:
            out = directory / f'{v.name}.parquet'
            self._con.execute(f"COPY ({self._solution_sql(v.name)}) TO '{out}' (FORMAT parquet)")
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
