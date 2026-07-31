"""Build a `Program` into `ModelTables` through duckdb.

The duckdb twin of `relational/executor.py`, and a **drop-in at the sink seam**:
it hands back the same `sinks.ModelTables` the polars executor does, so
`lp_file`, `solver_direct`, the status codes and the result readers are
untouched and unaware. That is what makes the two comparable — the only thing
that differs between a `PolarsExecutor` build and a `DuckExecutor` build is
which engine filled the four frames.

Scope: the affine core — variables with bounds and masks, constraints over
sum/group_sum/translate, one objective. Enough to build every model in
`bench/models/` and diff the result against polars, which is what pricing the
SQL is for. Not the whole language: piecewise expansion happens above this
layer anyway, and duals/solution read-back are the polars executor's business
since they are joins against label frames rather than engine work.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import polars as pl

from lpspec.errors import DataError, LanguageError, null_bounds_message
from lpspec.relational import plan, sinks
from lpspec.relational.duck.compiler import UNIT, DuckCompiler, Rel, TermFragment, _ordinal, lit, q

if TYPE_CHECKING:
    from collections.abc import Sequence

_COLS = ('col', 'lb', 'ub', 'vtype')
_ROWS = ('row', 'sense', 'rhs')
_MATRIX = ('row', 'col', 'coeff')
_OBJ = ('col', 'coeff')


class DuckExecutor:
    """`build()` then `_tables()`, the same two calls the polars executor answers."""

    def __init__(self) -> None:
        import duckdb

        self._con = duckdb.connect()
        self._compiler: DuckCompiler | None = None
        self._variables: dict[str, str] = {}
        self._constraints: dict[str, str] = {}
        self._n_cols = 0
        self._n_rows = 0
        self._obj_const = 0.0
        self._obj_sense = 'min'
        self._registered = 0

    # -- registration ---------------------------------------------------

    def _view(self, sql: str, prefix: str) -> str:
        """Materialise *sql* as a named table and return the name.

        Materialised rather than a view on purpose: a label relation is read
        several times downstream, and a view would re-execute the whole nested
        SELECT underneath it each time. The polars side makes the same call in
        the same places — `labels.frame` collects.
        """
        self._registered += 1
        name = f'{prefix}_{self._registered}'
        self._con.execute(f'CREATE TABLE {q(name)} AS {sql}')
        return name

    def _register(self, name: str, frame: pl.DataFrame) -> str:
        self._con.register(name, frame.to_arrow())
        return name

    def build(self, program: plan.Program, bound: Any) -> None:
        """Bind, then build every declaration into the model frames.

        *bound* is the polars lane's `BoundSources`. Reusing it is deliberate
        and is the one place this port does not stand alone: binding is
        `pl.scan_parquet` plus dtype and duplicate-coordinate validation, which
        is data-layer work rather than engine work, and porting it would price
        the parquet reader rather than the compiler. `bench/duckdb-spike.md` §7
        measures that half separately.
        """
        self._program = program
        dims = {n: self._register(f'dim_{n}', f.collect()) for n, f in bound.dimensions.items()}
        params = {n: self._register(f'par_{n}', f.collect()) for n, f in bound.parameters.items()}
        self._compiler = DuckCompiler(
            program, dims, params, bound.cardinality, bound.boolean_parameters, self._variables
        )

        cols = [self._build_variable(v) for v in program.variables]
        built = [self._build_constraint(c) for c in program.constraints]
        objective = self._build_objective(program.objective)

        self._cols = _stack(cols, _COLS)
        self._rows = _stack([r for r, _ in built], _ROWS)
        self._matrix = _stack([m for _, m in built if m is not None], _MATRIX)
        self._obj = _stack([objective] if objective is not None else [], _OBJ)

    @property
    def _q(self) -> DuckCompiler:
        assert self._compiler is not None, 'build() has not run'
        return self._compiler

    # -- labels ---------------------------------------------------------

    def _label_frame(
        self,
        dims: tuple[str, ...],
        where: plan.Predicate | None,
        label: str,
        start: int,
        restrictions: Sequence[tuple[tuple[str, ...], Rel]] = (),
    ) -> tuple[str, int]:
        """The masked coord product of *dims* with a dense *label* from *start*.

        Two paths, not the polars side's three. **Unmasked**, a row's label is
        its position in the product — arithmetic on the ordinals, no sort and
        nothing to count. **Otherwise** it is counted, which costs the ordered
        window. The `_factored` middle path is an optimisation rather than a
        semantics, and is left out here on purpose: it is the one place the
        polars engine is *algorithmically* ahead, so including a half version
        of it would flatter this port.
        """
        rel = self._q.frame(dims, where)
        if where is None and not restrictions:
            # row-major: the leading dim's ordinal times the width of the rest
            terms, stride = [], 1
            for d in reversed(dims):
                terms.append(f'{q(_ordinal(d))} * {stride}')
                stride *= self._q.cardinality[d]
            position = ' + '.join(reversed(terms)) if terms else '0'
            cols = ', '.join(q(d) for d in dims) or q(UNIT)
            sql = f'SELECT {cols}, ({position} + {start})::BIGINT AS {q(label)} FROM {rel.alias("p")}'
            name = self._view(sql, 'lbl')
            return name, start + math.prod(self._q.cardinality[d] for d in dims)

        carrier = rel
        for on, presence in restrictions:
            keep = ', '.join(f'l.{q(c)}' for c in carrier.columns)
            on_sql = ' AND '.join(f'l.{q(d)} IS NOT DISTINCT FROM r.{q(d)}' for d in on)
            carrier = Rel(
                f'SELECT {keep} FROM {carrier.alias("l")} WHERE EXISTS '
                f'(SELECT 1 FROM (SELECT DISTINCT {", ".join(q(d) for d in on)} FROM {presence.alias("p")}) AS r '
                f'WHERE {on_sql})',
                carrier.columns,
            )
        order = ', '.join(q(_ordinal(d)) for d in dims) or '1'
        cols = ', '.join(q(d) for d in dims) or q(UNIT)
        sql = (
            f'SELECT {cols}, (ROW_NUMBER() OVER (ORDER BY {order}) - 1 + {start})::BIGINT AS {q(label)} '
            f'FROM {carrier.alias("c")}'
        )
        name = self._view(sql, 'lbl')
        height = self._con.execute(f'SELECT count(*) FROM {q(name)}').fetchone()[0]
        return name, start + height

    # -- declarations ---------------------------------------------------

    def _build_variable(self, v: plan.VariableDeclaration) -> pl.DataFrame:
        """One variable's labelled relation, and its share of ``cols``."""
        name, self._n_cols = self._label_frame(v.dims, v.where, 'var_label', self._n_cols)
        self._variables[v.name] = name
        labelled = Rel(f'SELECT * FROM {q(name)}', (*v.dims, 'var_label'))

        bounded = self._q.bounds(labelled, v)
        sql = (
            f'SELECT var_label AS col, lb::DOUBLE AS lb, ub::DOUBLE AS ub, '
            f'{lit(v.variable_type)} AS vtype FROM {bounded.alias("b")}'
        )
        cols = self._fetch(sql)
        bad = cols.filter(pl.col('lb').is_null() | pl.col('ub').is_null()).height
        if bad:
            raise DataError(null_bounds_message(v.name, bad))
        return cols

    def _build_constraint(self, c: plan.ConstraintDeclaration) -> tuple[pl.DataFrame, pl.DataFrame | None]:
        """One constraint as its ``rows`` and its share of the matrix."""
        lhs = self._q.expression(c.lhs, f"constraint '{c.name}' lhs")
        rhs = self._q.expression(c.rhs, f"constraint '{c.name}' rhs")
        terms = [(p, 1.0) for p in lhs.terms] + [(p, -1.0) for p in rhs.terms]
        consts = [(p, 1.0) for p in rhs.consts] + [(p, -1.0) for p in lhs.consts]
        for p, _ in [*terms, *consts]:
            extra = set(p.dims) - set(c.dims)
            if extra:
                raise LanguageError(
                    f"constraint '{c.name}': expression has dims {sorted(extra)} outside "
                    f'foreach {list(c.dims)} — missing a Sum/GroupSum?'
                )

        restrictions = _absence_restrictions([p for p, _ in terms])
        name, self._n_rows = self._label_frame(c.dims, c.where, 'row', self._n_rows, restrictions)
        self._constraints[c.name] = name
        frame = Rel(f'SELECT * FROM {q(name)}', (*c.dims, 'row'))

        carrier, accumulated = frame, []
        for i, (p, sign) in enumerate(consts):
            column = f'__const {i}__'
            aggregated = self._q.constant_scalar(p)
            keep = ', '.join(f'l.{q(x)}' for x in carrier.columns)
            if p.dims:
                on = ' AND '.join(f'l.{q(d)} IS NOT DISTINCT FROM r.{q(d)}' for d in p.dims)
                join = f'{carrier.alias("l")} LEFT JOIN {aggregated.alias("r")} ON {on}'
            else:
                join = f'{carrier.alias("l")} CROSS JOIN {aggregated.alias("r")}'
            carrier = Rel(f'SELECT {keep}, r.cval AS {q(column)} FROM {join}', (*carrier.columns, column))
            accumulated.append(f'{lit(sign)} * coalesce({q(column)}, 0.0)')
        total = ' + '.join(accumulated) or '0.0'
        rows = self._fetch(f'SELECT row, {lit(c.sense)} AS sense, ({total})::DOUBLE AS rhs FROM {carrier.alias("r")}')

        if not terms:
            return rows, None

        pieces = []
        for p, sign in terms:
            if p.dims:
                on = ' AND '.join(f'l.{q(d)} IS NOT DISTINCT FROM r.{q(d)}' for d in p.dims)
                join = f'{frame.alias("l")} JOIN {p.rel.alias("r")} ON {on}'
            else:
                join = f'{frame.alias("l")} CROSS JOIN {p.rel.alias("r")}'
            pieces.append(
                f'SELECT l.row AS row, r.var_label AS col, ({lit(sign)} * r.coeff)::DOUBLE AS coeff FROM {join}'
            )
        stacked = ' UNION ALL '.join(f'({s})' for s in pieces)
        if not _needs_aggregate([f for f, _ in terms]):
            return rows, self._fetch(stacked)
        # `sum` over `(row, col)` is the terminal aggregate — where duplicates
        # from Sum and GroupSum, which project rather than aggregate, collapse.
        return rows, self._fetch(
            f'SELECT row, col, sum(coeff) AS coeff FROM ({stacked}) GROUP BY row, col ORDER BY row, col'
        )

    def _build_objective(self, o: plan.ObjectiveDeclaration) -> pl.DataFrame | None:
        """The objective as ``(col, coeff)``, or ``None`` if it has no terms."""
        comp = self._q.expression(o.expression, 'objective')
        for p in comp.consts:
            if p.dims:
                raise LanguageError(
                    'objective constant part has dims — wrap parameter terms in '
                    'Mul with a Var, or pre-aggregate to a scalar'
                )
            got = self._con.execute(f'SELECT sum(cval) FROM {p.rel.alias("c")}').fetchone()[0]
            self._obj_const += got or 0.0
        self._obj_sense = o.sense
        if not comp.terms:
            return None
        pieces = [f'(SELECT var_label AS col, coeff FROM {p.rel.alias("o")})' for p in comp.terms]
        stacked = ' UNION ALL '.join(pieces)
        if _needs_aggregate(comp.terms, projected=True):
            return self._fetch(f'SELECT col, sum(coeff) AS coeff FROM ({stacked}) GROUP BY col')
        return self._fetch(stacked)

    # -- the sink seam --------------------------------------------------

    def _fetch(self, sql: str) -> pl.DataFrame:
        """One query out, as the polars frame the sinks read."""
        out = pl.from_arrow(self._con.execute(sql).arrow())
        assert isinstance(out, pl.DataFrame)
        return out

    def _tables(self) -> sinks.ModelTables:
        return sinks.ModelTables(
            cols=self._cols,
            obj=self._obj,
            rows=self._rows,
            matrix=self._matrix,
            column_count=self._n_cols,
            row_count=self._n_rows,
            objective_sense=self._obj_sense,
            objective_constant=self._obj_const,
        )

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> DuckExecutor:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False


def _stack(frames: list[pl.DataFrame], columns: tuple[str, ...]) -> pl.DataFrame:
    kept = [f for f in frames if f.height]
    if not kept:
        return pl.DataFrame(
            schema={c: (pl.String if c in ('sense', 'vtype') else pl.Float64) for c in columns}
        ).with_columns([pl.col(c).cast(pl.Int64) for c in columns if c in ('row', 'col')])
    return pl.concat([f.select(columns) for f in kept])


def _needs_aggregate(terms: Sequence[TermFragment], *, projected: bool = False) -> bool:
    """Whether two rows can land on one ``(row, col)`` — see the polars twin."""
    if len(terms) > 1:
        return True
    return any(not (p.keyed and (not projected or p.label_dims >= set(p.dims))) for p in terms)


def _absence_restrictions(terms: Sequence[TermFragment]) -> list[tuple[tuple[str, ...], Rel]]:
    """Where a masked variable says a constraint row must not exist."""
    return [(p.presence_dims or p.dims, p.presence) for p in terms if p.presence is not None]
