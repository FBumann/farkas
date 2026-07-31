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
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import duckdb
import polars as pl

from lpspec.errors import DataError, LanguageError, null_bounds_message
from lpspec.relational import plan, sinks
from lpspec.relational.binding import bind
from lpspec.relational.engine import Engine
from lpspec.relational.engines.duck.compiler import UNIT, DuckCompiler, Rel, TermFragment, _ordinal, lit, q

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

#: The four frames a sink reads, and their dtypes — both stated by
#: `sinks/tables.py`, which is what reads them. Shared with the polars engine
#: for the reason the frames are: a sink cannot see which engine filled them.
_COLS, _OBJ, _ROWS, _MATRIX = sinks.COLS, sinks.OBJ, sinks.ROWS, sinks.MATRIX


class _Labels(Mapping[str, 'pl.LazyFrame']):
    """Label relations, fetched out of duckdb only if a read-back asks.

    `Engine` reads a solution back by joining the solver's answer onto
    `(dims…, label)` frames, and states those as polars. Materialising every
    one at build time would put a second copy of the labels in this process —
    which is most of what choosing this engine was for. So the name is held
    and the frame is fetched on the first access, which for a caller that only
    writes an LP file never happens.
    """

    def __init__(self, con: duckdb.DuckDBPyConnection, tables: dict[str, str]) -> None:
        self._con = con
        self._tables = tables
        self._frames: dict[str, pl.LazyFrame] = {}

    def __getitem__(self, name: str) -> pl.LazyFrame:
        if name not in self._frames:
            self._frames[name] = self._con.execute(f'SELECT * FROM {q(self._tables[name])}').pl().lazy()
        return self._frames[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._tables)

    def __len__(self) -> int:
        return len(self._tables)

    def clear(self) -> None:
        """Drop the cached frames; the relations go with the connection."""
        self._frames.clear()


class DuckExecutor(Engine):
    """The duckdb engine: plan → SQL → `sinks.ModelTables`.

    Everything past the four frames — both sinks, the solution read-back, the
    context manager — comes from `Engine` and is shared with the polars
    executor, because none of it is engine work.
    """

    _cols: pl.DataFrame | None
    _obj: pl.DataFrame | None
    _rows: pl.DataFrame | None
    _matrix: pl.DataFrame | None
    _con: duckdb.DuckDBPyConnection

    def __init__(self) -> None:
        self._con = duckdb.connect()
        self._compiler: DuckCompiler | None = None
        self._program: plan.Program | None = None
        self._var_tables: dict[str, str] = {}
        self._row_tables: dict[str, str] = {}
        #: the dims each declared name is read through — what `plan.free_prefix`
        #: needs to know which label path a mask allows
        self._name_dims: dict[str, tuple[str, ...]] = {}
        self._var_labels = _Labels(self._con, self._var_tables)
        self._row_labels = _Labels(self._con, self._row_tables)
        #: the contiguous run of labels each declaration was given — what
        #: `Engine._read_back` slices a solver vector by, on both engines
        self._blocks: dict[str, tuple[int, int]] = {}
        self._n_cols = 0
        self._n_rows = 0
        self._obj_const = 0.0
        self._obj_sense = 'min'
        self._registered = 0

    # -- registration ---------------------------------------------------

    def _relation(self, sql: str, prefix: str, *, materialise: bool) -> str:
        """Name *sql* as a table or a view, and return the name.

        **Materialise when the derivation is the expensive part, not when the
        result is read often.** A label relation is read three or four times
        downstream, which argues for a table until you price the two sides: a
        counted label needs an ordered window over the whole coordinate product
        and is worth paying for once, while an arithmetic one is a cross join
        of two small relations and a multiply. Writing ten million rows to
        answer that a second time costs more than answering it three times.

        The polars engine collects in the same places, and for a reason that
        does not carry over: a `LazyFrame` read twice is *planned* twice, and
        the plan under a label reaches all the way back to the parquet.
        """
        self._registered += 1
        name = f'{prefix}_{self._registered}'
        self._con.execute(f'CREATE {"TABLE" if materialise else "VIEW"} {q(name)} AS {sql}')
        return name

    def _register(self, name: str, frame: pl.DataFrame) -> str:
        """Copy *frame* into a table of its own, and return the table's name.

        **Copied, not scanned in place.** A registered frame stays a Python
        object: duckdb reads it through the buffer protocol from whichever
        worker thread the scan lands on, and those threads need the GIL — which
        the thread that called `execute` is holding while it waits. On a plan
        with several scans of registered frames feeding one pipeline, that
        deadlocks outright: `transport/l` reproduces it as a build that sits at
        0% CPU indefinitely, while the same query over copies returns in 0.3 s.

        Copying is not free — the frame and the table are both resident for the
        length of one statement — but it buys a build that cannot stall, and
        duckdb's own storage for every scan after the first.

        The frame itself, not `to_arrow()`: duckdb reads polars natively, and
        the round-trip through a pyarrow table is what used to drag pandas into
        a runtime that declares it a bridge out and not a dependency.
        """
        source = f'__source {name}__'
        self._con.register(source, frame)
        try:
            self._con.execute(f'CREATE TABLE {q(name)} AS SELECT * FROM {q(source)}')
        finally:
            self._con.unregister(source)
        return name

    def build(self, program: plan.Program, sources: Mapping[str, Any]) -> None:
        """Bind *sources*, then build every declaration into the model frames.

        Binding is `relational/binding.py`, shared with the polars engine: it
        is `scan_parquet` plus dtype and duplicate-coordinate validation, which
        is what a caller's data has to survive whoever builds the model.
        """
        self._program = program
        self._name_dims = plan.name_dims(program)
        bound = bind(program, sources)
        dims = {n: self._register(f'dim_{n}', f.collect()) for n, f in bound.dimensions.items()}
        params = {n: self._register(f'par_{n}', f.collect()) for n, f in bound.parameters.items()}
        self._compiler = DuckCompiler(
            program, dims, params, bound.cardinality, bound.boolean_parameters, self._var_tables
        )

        cols = [self._build_variable(v) for v in program.variables]
        built = [self._build_constraint(c) for c in program.constraints]
        objective = self._build_objective(program.objective)

        self._cols = _stack(cols, _COLS)
        self._rows = _stack([r for r, _ in built], _ROWS)
        self._matrix = _stack([m for _, m in built if m is not None], _MATRIX)
        self._obj = _stack([objective] if objective is not None else [], _OBJ)

    @property
    def _variables(self) -> Mapping[str, pl.LazyFrame]:
        return self._var_labels

    @property
    def _constraints(self) -> Mapping[str, pl.LazyFrame]:
        return self._row_labels

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

        The same three paths the polars engine has, chosen by the same
        function. **Unmasked**, a row's label is its position in the product —
        arithmetic on the ordinals, no sort and nothing to count. **Masked but
        factoring**, the survivors are a rectangle and the label is arithmetic
        again over a ranked survivor set (:meth:`_factored`). **Otherwise** it
        is counted, which costs the ordered window over the whole product.

        `plan.free_prefix` decides between them for both engines, because a
        label is the solver's own column index: two engines choosing routes
        independently is how they would come to build different models.
        """
        if not restrictions:
            if where is None:
                rows = math.prod(self._q.cardinality[d] for d in dims)
                cols = ', '.join(q(d) for d in dims) or q(UNIT)
                rel = self._q.frame(dims, None)
                sql = f'SELECT {cols}, ({self._row_major(dims, start)})::BIGINT AS {q(label)} FROM {rel.alias("p")}'
                return self._relation(sql, 'lbl', materialise=False), start + rows

            free = plan.free_prefix(dims, plan.predicate_dims(where, self._name_dims))
            if free:
                return self._factored(dims, free, where, label, start)

        carrier = self._q.frame(dims, where)
        for on, presence in restrictions:
            keep = ', '.join(f'l.{q(c)}' for c in carrier.columns)
            on_sql = ' AND '.join(f'l.{q(d)} IS NOT DISTINCT FROM r.{q(d)}' for d in on)
            carrier = Rel(
                f'SELECT {keep} FROM {carrier.alias("l")} WHERE EXISTS '
                f'(SELECT 1 FROM (SELECT DISTINCT {", ".join(q(d) for d in on)} FROM {presence.alias("p")}) AS r '
                f'WHERE {on_sql})',
                carrier.columns,
            )
        return self._counted(carrier, dims, label, start)

    def _row_major(self, dims: tuple[str, ...], start: int, alias: str = '') -> str:
        """Row-major position in *dims*' coordinate product, offset by *start*.

        The trailing dim has stride 1 and every other is the product of the
        cardinalities to its right, so the position is a dot product against
        the ordinals the frame already carries — no ordering imposed, because
        the answer does not depend on the order rows arrive in. The polars twin
        is `Labeller.row_major`, and both arithmetic paths reach a label
        through it for the reason the label itself is written once.
        """
        prefix = f'{alias}.' if alias else ''
        terms: list[str] = []
        stride = 1
        for d in reversed(dims):
            terms.append(f'{prefix}{q(_ordinal(d))} * {stride}')
            stride *= self._q.cardinality[d]
        return ' + '.join([*reversed(terms), str(start)])

    def _counted(self, carrier: Rel, dims: tuple[str, ...], label: str, start: int) -> tuple[str, int]:
        """Rank the surviving rows of *carrier*: the ordered window, and a count.

        The general answer and the expensive one — a global sort of the whole
        product, which is what the two arithmetic paths exist to avoid.
        """
        order = ', '.join(q(_ordinal(d)) for d in dims) or '1'
        cols = ', '.join(q(d) for d in dims) or q(UNIT)
        sql = (
            f'SELECT {cols}, (ROW_NUMBER() OVER (ORDER BY {order}) - 1 + {start})::BIGINT AS {q(label)} '
            f'FROM {carrier.alias("c")}'
        )
        name = self._relation(sql, 'lbl', materialise=True)
        return name, start + self._height(name)

    def _factored(
        self,
        dims: tuple[str, ...],
        free: int,
        where: plan.Predicate,
        label: str,
        start: int,
    ) -> tuple[str, int]:
        """Labels for a mask that reads none of the first *free* dims.

        A mask that cannot see the leading dims removes the same coordinates
        under every one of their values, so the survivors are a rectangle: the
        full product of the leading dims against one surviving set. Ranking
        that set costs a window over the *set*, not over the product — on
        `dispatch` it ranks 100 generators instead of 10M
        ``(snapshot, generator)`` pairs, and the window is what a global sort
        made the dominant cost of the build.

        The label is then arithmetic again, through the same
        :meth:`_row_major` the unmasked path uses: row-major over the leading
        dims, times the width of the surviving set, plus a survivor's rank
        within it. That is the same number the window would have counted,
        because for each leading coordinate the same survivors appear in the
        same order — which is what `tests/test_engine_parity.py` checks by
        comparing the built model against the other engine's.
        """
        head, kept = dims[:free], dims[free:]
        ranked = self._relation(
            f'SELECT {", ".join(q(d) for d in kept)}, '
            f'(ROW_NUMBER() OVER (ORDER BY {", ".join(q(_ordinal(d)) for d in kept)}) - 1)::BIGINT AS "__rank" '
            f'FROM {self._q.frame(kept, where).alias("k")}',
            'srv',
            # the one relation here worth writing down: it is small, it is
            # counted, and its window is the work the rectangle exists to avoid
            materialise=True,
        )
        width = self._height(ranked)
        if width == 0:
            # nothing survived anywhere, so there is no rectangle to describe.
            # The counted path returns the right columns and dtypes for free.
            return self._counted(self._q.frame(dims, where), dims, label, start)

        product = self._q.frame(head, None)
        picked = ', '.join([*(f'h.{q(d)}' for d in head), *(f's.{q(d)}' for d in kept)])
        position = self._row_major(head, 0, alias='h')
        sql = (
            f'SELECT {picked}, (({position}) * {width} + s."__rank" + {start})::BIGINT AS {q(label)} '
            f'FROM {product.alias("h")} CROSS JOIN {q(ranked)} AS s'
        )
        rows = math.prod(self._q.cardinality[d] for d in head) * width
        return self._relation(sql, 'lbl', materialise=False), start + rows

    def _height(self, table: str) -> int:
        got = self._con.execute(f'SELECT count(*) FROM {q(table)}').fetchone()
        assert got is not None
        return int(got[0])

    # -- declarations ---------------------------------------------------

    def _build_variable(self, v: plan.VariableDeclaration) -> pl.DataFrame:
        """One variable's labelled relation, and its share of ``cols``."""
        start = self._n_cols
        name, self._n_cols = self._label_frame(v.dims, v.where, 'var_label', start)
        self._var_tables[v.name] = name
        self._blocks[v.name] = (start, self._n_cols - start)
        labelled = Rel(f'SELECT * FROM {q(name)}', (*v.dims, 'var_label'))

        bounded = self._q.bounds(labelled, v)
        sql = f'SELECT var_label AS col, lb::DOUBLE AS lb, ub::DOUBLE AS ub FROM {bounded.alias("b")}'
        # `vtype` is attached here rather than selected as a literal in SQL:
        # one word per column is one *copy* of that word per row over the wire,
        # and the frame's stated dtype is an Enum holding four bytes.
        cols = self._fetch(sql).with_columns(pl.lit(v.variable_type, dtype=sinks.VTYPE).alias('vtype'))
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
        start = self._n_rows
        name, self._n_rows = self._label_frame(c.dims, c.where, 'row', start, restrictions)
        self._row_tables[c.name] = name
        self._blocks[c.name] = (start, self._n_rows - start)
        frame = Rel(f'SELECT * FROM {q(name)}', (*c.dims, 'row'))

        carrier = frame
        accumulated: list[str] = []
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
        # Unordered: every sink sorts the matrix into the order it needs
        # (`lp_file` by `(row, col)`, `solver_direct` by `row`), so an ORDER BY
        # here is a second sort of the largest frame in the model for nothing.
        return rows, self._fetch(f'SELECT row, col, sum(coeff) AS coeff FROM ({stacked}) GROUP BY row, col')

    def _build_objective(self, o: plan.ObjectiveDeclaration) -> pl.DataFrame | None:
        """The objective as ``(col, coeff)``, or ``None`` if it has no terms."""
        comp = self._q.expression(o.expression, 'objective')
        for p in comp.consts:
            if p.dims:
                raise LanguageError(
                    'objective constant part has dims — wrap parameter terms in '
                    'Mul with a Var, or pre-aggregate to a scalar'
                )
            row = self._con.execute(f'SELECT sum(cval) FROM {p.rel.alias("c")}').fetchone()
            self._obj_const += (row[0] if row else None) or 0.0
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
        return self._con.execute(sql).pl()

    def _tables(self) -> sinks.ModelTables:
        assert self._cols is not None and self._obj is not None
        assert self._rows is not None and self._matrix is not None
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
        """Drop the built model. Calling twice is not an error."""
        self._cols = self._obj = self._rows = self._matrix = None
        self._var_labels.clear()
        self._row_labels.clear()
        self._blocks.clear()
        self._compiler = None
        self._con.close()


def _stack(frames: list[pl.DataFrame], columns: tuple[str, ...]) -> pl.DataFrame:
    kept = [f for f in frames if f.height]
    if not kept:
        return pl.DataFrame(schema={name: sinks.DTYPES[name] for name in columns})
    return pl.concat([f.select(columns) for f in kept])


def _needs_aggregate(terms: Sequence[TermFragment], *, projected: bool = False) -> bool:
    """Whether two rows can land on one ``(row, col)`` — see the polars twin."""
    if len(terms) > 1:
        return True
    return any(not (p.keyed and (not projected or p.label_dims >= set(p.dims))) for p in terms)


def _absence_restrictions(terms: Sequence[TermFragment]) -> list[tuple[tuple[str, ...], Rel]]:
    """Where a masked variable says a constraint row must not exist."""
    return [(p.presence_dims or p.dims, p.presence) for p in terms if p.presence is not None]
