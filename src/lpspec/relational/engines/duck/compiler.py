"""Logical plan → SQL. The duckdb twin of `relational/compiler.py`.

Written to be read **beside** its polars original, not instead of it: the
method names, the argument order and the column conventions are the same, so a
reviewer can put the two files side by side and see what the engine choice
costs. That is the whole point of this module existing — `bench/duckdb-spike.md`
priced the port by counting lines, and a count cannot say whether the result is
readable.

Column conventions, identical to the polars compiler:

===================  ==========================================
relation             columns
===================  ==========================================
dimension table      ``val``, ``ord``, plus declared coordinates
parameter table      ``dims…``, ``value``
variable relation    ``dims…``, ``var_label``
term fragment        ``dims…``, ``var_label``, ``coeff``
const fragment       ``dims…``, ``cval``
===================  ==========================================

**An identifier is a value here, never syntax** — the same rule the polars
compiler states, and the one that cost the original engine a `sql.py` module
and an identifier restriction (#189 deleted both). Every name that reaches SQL
goes through :func:`q`, which quotes it. Nothing is interpolated raw.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from lpspec.errors import LanguageError
from lpspec.relational import plan

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

#: Scratch columns. The spaces make them unrepresentable as declared names, so
#: they cannot collide with a dimension or coordinate the model already has.
_RHS = '__rhs value__'
UNIT = '__unit__'


def q(name: str) -> str:
    """A name as a SQL identifier. The only way one may reach a query."""
    return '"' + name.replace('"', '""') + '"'


def lit(value: object) -> str:
    """A Python value as a SQL literal, quoted if it is a string."""
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if value is None:
        return 'NULL'
    if isinstance(value, float) and not math.isfinite(value):
        return "'Infinity'::DOUBLE" if value > 0 else "'-Infinity'::DOUBLE"
    return repr(value)


def _ordinal(dim: str) -> str:
    return f'__ord {dim}__'


@dataclass(frozen=True)
class Rel:
    """A SELECT, and the columns it is known to carry.

    Composition is by nesting rather than by CTE. A CTE would read better in
    isolation and worse in aggregate: every operator would have to invent a
    unique name, and the names would then appear in the plan of every operator
    above it. Nesting keeps a fragment a value, which is what the polars side
    gets for free from `LazyFrame`.
    """

    sql: str
    columns: tuple[str, ...]

    def alias(self, name: str) -> str:
        return f'({self.sql}) AS {q(name)}'


@dataclass(frozen=True)
class TermFragment:
    """One additive piece of a compiled affine expression.

    The same shape as the polars compiler's, field for field, because the
    executor above it must not be able to tell which engine filled it.
    """

    dims: tuple[str, ...]
    rel: Rel
    is_term: bool
    keyed: bool = True
    label_dims: frozenset[str] = frozenset()
    presence: Rel | None = None
    presence_dims: tuple[str, ...] | None = None

    @property
    def value_column(self) -> str:
        return 'coeff' if self.is_term else 'cval'

    @property
    def carried(self) -> list[str]:
        return ['var_label', self.value_column] if self.is_term else [self.value_column]

    def survives_dropping(self, dropped: set[str]) -> bool:
        return self.keyed and dropped <= self.label_dims


@dataclass(frozen=True)
class CompiledExpression:
    """Terms and constant parts, kept apart until assembly."""

    terms: tuple[TermFragment, ...] = ()
    consts: tuple[TermFragment, ...] = ()


@dataclass
class DuckCompiler:
    """Plan → SQL, against relations already registered on *con*."""

    program: plan.Program
    dimensions: Mapping[str, str]
    parameters: Mapping[str, str]
    cardinality: Mapping[str, int]
    boolean_parameters: frozenset[str]
    variables: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # frames — the masked coordinate product a declaration is instantiated over
    # ------------------------------------------------------------------

    def frame(self, dims: tuple[str, ...], where: plan.Predicate | None) -> Rel:
        """The masked coordinate product over *dims*: labels, plus the ordinals
        a caller sorts by so labels follow declaration order."""
        out = self._coordinate_product(dims)
        if where is None:
            return out
        out, condition = self._predicate(out, where, dims)
        keep = ', '.join(q(c) for c in out.columns if not c.startswith('__where'))
        cols = tuple(c for c in out.columns if not c.startswith('__where'))
        # `coalesce(cond, FALSE)`: SQL three-valued logic makes a null mask
        # neither true nor false, and a row a mask cannot judge is absent —
        # the polars side spells this `_falsy_if_null`.
        return Rel(f'SELECT {keep} FROM {out.alias("m")} WHERE coalesce({condition}, FALSE)', cols)

    def _coordinate_product(self, dims: tuple[str, ...]) -> Rel:
        """Cross join of the dim tables: labels and ordinals, nothing else."""
        if not dims:
            # The empty cross join's unit is one row, not nothing — and the row
            # has to be real, since a `where` on a scalar declaration filters
            # this relation and nothing survives a filter.
            return Rel(f'SELECT 0 AS {q(UNIT)}', (UNIT,))
        selects: list[str] = []
        froms: list[str] = []
        for i, d in enumerate(dims):
            t = f'd{i}'
            selects += [f'{t}.val AS {q(d)}', f'{t}.ord AS {q(_ordinal(d))}']
            froms.append(f'{q(self.dimensions[d])} AS {t}')
        cols = tuple(c for d in dims for c in (d, _ordinal(d)))
        return Rel(f'SELECT {", ".join(selects)} FROM ' + ' CROSS JOIN '.join(froms), cols)

    def parameter_join(self, rel: Rel, param: str, frame_dims: tuple[str, ...], alias: str, subject: str) -> Rel:
        """Left-join *param* onto *rel*, its value column renamed to *alias*."""
        declaration = self.program.parameter(param)
        extra = set(declaration.dims) - set(frame_dims)
        if extra:
            raise LanguageError(f'{subject} has dims {sorted(extra)} outside the foreach dims {list(frame_dims)}')
        carried = ', '.join(f'l.{q(c)}' for c in rel.columns)
        table = q(self.parameters[param])
        if not declaration.dims:
            join = f'{rel.alias("l")} CROSS JOIN {table} AS r'
        else:
            on = ' AND '.join(f'l.{q(d)} IS NOT DISTINCT FROM r.{q(d)}' for d in declaration.dims)
            join = f'{rel.alias("l")} LEFT JOIN {table} AS r ON {on}'
        return Rel(f'SELECT {carried}, r.value AS {q(alias)} FROM {join}', (*rel.columns, alias))

    # ------------------------------------------------------------------
    # predicates (where masks — row absence)
    # ------------------------------------------------------------------

    def _predicate(self, rel: Rel, pred: plan.Predicate, dims: tuple[str, ...]) -> tuple[Rel, str]:
        """``(rel with the mask's parameters joined, boolean SQL)``."""
        joined: set[str] = set()
        carrier = rel

        def join_param(param: str) -> str:
            nonlocal carrier
            alias = f'__where {param}__'
            if alias not in joined:
                carrier = self.parameter_join(carrier, param, dims, alias, f"where-parameter '{param}'")
                joined.add(alias)
            return alias

        def walk(p: plan.Predicate) -> str:
            if isinstance(p, plan.ParameterComparison):
                return _compare(f'm.{q(join_param(p.parameter))}', p.op, p.value)
            if isinstance(p, plan.DimensionComparison):
                if p.dimension not in dims:
                    raise LanguageError(
                        f"where-comparison on dimension '{p.dimension}' is outside the foreach dims "
                        f'{list(dims)} — reducing a mask over an unlisted dim is not supported'
                    )
                return _compare(f'm.{q(p.dimension)}', p.op, p.value)
            if isinstance(p, plan.ParameterDefined):
                col = f'm.{q(join_param(p.parameter))}'
                if p.parameter in self.boolean_parameters:
                    return f'({col} IS NOT NULL AND {col}::BOOLEAN)'
                return f'({col} IS NOT NULL AND isfinite({col}))'
            if isinstance(p, plan.VariableDefined):
                nonlocal carrier
                flag = f'__where defined {p.variable}__'
                if flag not in joined:
                    on_dims = list(self.program.variable(p.variable).dims)
                    carried = ', '.join(f'l.{q(c)}' for c in carrier.columns)
                    marked = (
                        f'(SELECT DISTINCT {", ".join(q(d) for d in on_dims)}, TRUE AS {q(flag)} '
                        f'FROM {q(self.variables[p.variable])}) AS r'
                    )
                    on = ' AND '.join(f'l.{q(d)} IS NOT DISTINCT FROM r.{q(d)}' for d in on_dims)
                    carrier = Rel(
                        f'SELECT {carried}, r.{q(flag)} FROM {carrier.alias("l")} LEFT JOIN {marked} ON {on}',
                        (*carrier.columns, flag),
                    )
                    joined.add(flag)
                return f'coalesce(m.{q(flag)}, FALSE)'
            if isinstance(p, plan.BooleanConstant):
                return 'TRUE' if p.value else 'FALSE'
            if isinstance(p, plan.And):
                return f'({walk(p.left)} AND {walk(p.right)})'
            if isinstance(p, plan.Or):
                return f'({walk(p.left)} OR {walk(p.right)})'
            if isinstance(p, plan.Not):
                return f'(NOT coalesce({walk(p.operand)}, FALSE))'
            raise LanguageError(f'unsupported predicate node {type(p).__name__}')

        condition = walk(pred)
        return carrier, condition

    # ------------------------------------------------------------------
    # bounds
    # ------------------------------------------------------------------

    def bounds(self, rel: Rel, v: plan.VariableDeclaration) -> Rel:
        """*rel* with ``lb``/``ub`` columns for variable *v*.

        Joins and arithmetic are one object, so a bound cannot be evaluated
        against a relation missing what it reads.
        """
        carrier = rel
        joined: set[str] = set()

        def walk(e: plan.Expression) -> str:
            nonlocal carrier
            if isinstance(e, plan.Constant):
                return f'{lit(float(e.value))}::DOUBLE'
            if isinstance(e, plan.Parameter):
                alias = f'__bound {e.name}__'
                if alias not in joined:
                    carrier = self.parameter_join(
                        carrier, e.name, v.dims, alias, f"bound parameter '{e.name}' of variable '{v.name}'"
                    )
                    joined.add(alias)
                return f'b.{q(alias)}::DOUBLE'
            if isinstance(e, plan.Negate):
                return f'(-{walk(e.operand)})'
            if isinstance(e, plan.Add):
                return f'({walk(e.left)} + {walk(e.right)})'
            if isinstance(e, plan.Multiply):
                return f'({walk(e.left)} * {walk(e.right)})'
            raise LanguageError(
                f"unsupported node {type(e).__name__} in bounds of variable '{v.name}' "
                f'(bounds must be variable-free arithmetic over Constant/Parameter)'
            )

        lower, upper = walk(v.lower), walk(v.upper)
        carried = ', '.join(f'b.{q(c)}' for c in rel.columns)
        return Rel(
            f'SELECT {carried}, {lower} AS lb, {upper} AS ub FROM {carrier.alias("b")}',
            (*rel.columns, 'lb', 'ub'),
        )

    # ------------------------------------------------------------------
    # expressions
    # ------------------------------------------------------------------

    def expression(self, expr: plan.Expression, context: str) -> CompiledExpression:
        if isinstance(expr, plan.Constant):
            return CompiledExpression(consts=(self._constant_fragment(expr.value),))
        if isinstance(expr, plan.Parameter):
            return CompiledExpression(consts=(self._parameter_fragment(expr.name),))
        if isinstance(expr, plan.Variable):
            return CompiledExpression(terms=(self._variable_fragment(expr.name),))
        if isinstance(expr, plan.Negate):
            inner = self.expression(expr.operand, context)
            return CompiledExpression(tuple(_negate(p) for p in inner.terms), tuple(_negate(p) for p in inner.consts))
        if isinstance(expr, plan.Add):
            left, right = self.expression(expr.left, context), self.expression(expr.right, context)
            return CompiledExpression(left.terms + right.terms, left.consts + right.consts)
        if isinstance(expr, plan.Multiply):
            return self._product(self.expression(expr.left, context), self.expression(expr.right, context), context)
        if isinstance(expr, plan.Divide):
            return self._quotient(
                self.expression(expr.numerator, context), self.expression(expr.divisor, context), context
            )
        if isinstance(expr, plan.Sum):
            inner = self.expression(expr.operand, context)
            return _propagate_absence(
                CompiledExpression(
                    tuple(self._sum_fragment(p, expr.over, context) for p in inner.terms),
                    tuple(self._sum_fragment(p, expr.over, context) for p in inner.consts),
                )
            )
        if isinstance(expr, plan.GroupSum):
            inner = self.expression(expr.operand, context)
            return _propagate_absence(
                CompiledExpression(
                    tuple(self._group_fragment(p, expr, context) for p in inner.terms),
                    tuple(self._group_fragment(p, expr, context) for p in inner.consts),
                )
            )
        if isinstance(expr, plan.Translate):
            inner = self.expression(expr.operand, context)
            return CompiledExpression(
                tuple(self._translate_fragment(p, expr, context) for p in inner.terms),
                tuple(self._translate_fragment(p, expr, context) for p in inner.consts),
            )
        raise LanguageError(f'unsupported expression node {type(expr).__name__} in {context}')

    def _constant_fragment(self, value: float) -> TermFragment:
        return TermFragment((), Rel(f'SELECT {lit(float(value))} AS cval', ('cval',)), False)

    def _parameter_fragment(self, name: str) -> TermFragment:
        dims = self.program.parameter(name).dims
        cols = ', '.join(q(d) for d in dims)
        select = f'SELECT {cols + ", " if cols else ""}value AS cval FROM {q(self.parameters[name])}'
        return TermFragment(dims, Rel(select, (*dims, 'cval')), False)

    def _variable_fragment(self, name: str) -> TermFragment:
        dims = self.program.variable(name).dims
        # a scalar declaration has no dims, and `SELECT , var_label` is a
        # syntax error rather than an empty projection
        prefix = ''.join(f'{q(d)}, ' for d in dims)
        table = q(self.variables[name])
        rel = Rel(f'SELECT {prefix}var_label, 1.0 AS coeff FROM {table}', (*dims, 'var_label', 'coeff'))
        masked = self.program.variable(name).where is not None
        presence = Rel(f'SELECT {", ".join(q(d) for d in dims) or q(UNIT)} FROM {table}', dims) if masked else None
        return TermFragment(
            dims, rel, True, label_dims=frozenset(dims), presence=presence, presence_dims=dims if masked else None
        )

    def _product(self, a: CompiledExpression, b: CompiledExpression, context: str) -> CompiledExpression:
        if a.terms and b.terms:
            raise LanguageError(f'nonlinear product in {context}: both factors contain variables')
        if b.terms:
            a, b = b, a
        return CompiledExpression(
            tuple(_join_mul(p, c, is_term=True) for p in a.terms for c in b.consts),
            tuple(_join_mul(p, c, is_term=False) for p in a.consts for c in b.consts),
        )

    def _quotient(self, a: CompiledExpression, b: CompiledExpression, context: str) -> CompiledExpression:
        if b.terms:
            raise LanguageError(f'division by a variable in {context}')
        return CompiledExpression(
            tuple(_join_mul(p, c, is_term=True, divide=True) for p in a.terms for c in b.consts),
            tuple(_join_mul(p, c, is_term=False, divide=True) for p in a.consts for c in b.consts),
        )

    def _sum_fragment(self, p: TermFragment, over: tuple[str, ...], context: str) -> TermFragment:
        """Drop the summed dims. **Not an aggregate.**"""
        missing = [d for d in over if d not in p.dims]
        if missing and not p.is_term:
            raise LanguageError(
                f'in {context}: Sum over {list(over)} of a constant part lacking dims '
                f'{missing} is ambiguous under masks — multiply explicitly instead'
            )
        keep = tuple(d for d in p.dims if d not in over)
        dropped = {d for d in p.dims if d not in keep}
        scale = math.prod(self.cardinality[d] for d in missing)
        value = f'{q(p.value_column)}' if scale == 1 else f'{q(p.value_column)} * {lit(float(scale))}'
        carried = [q(c) for c in p.carried[:-1]] + [f'{value} AS {q(p.value_column)}']
        cols = ', '.join([*(q(d) for d in keep), *carried])
        rel = Rel(f'SELECT {cols} FROM {p.rel.alias("s")}', (*keep, *p.carried))
        return TermFragment(keep, rel, p.is_term, p.survives_dropping(dropped), p.label_dims - dropped)

    def _group_fragment(self, p: TermFragment, g: plan.GroupSum, context: str) -> TermFragment:
        """Relabel dim ``over`` to ``into`` through a declared coordinate."""
        if g.over not in p.dims:
            raise LanguageError(f"in {context}: GroupSum over '{g.over}' but the expression has dims {list(p.dims)}")
        keep = tuple(x for x in p.dims if x != g.over)
        carried = ', '.join(f'l.{q(c)}' for c in p.carried)
        kept = ''.join(f'l.{q(d)}, ' for d in keep)
        rel = Rel(
            f'SELECT {kept}r.{q(g.coordinate)} AS {q(g.into)}, {carried} '
            f'FROM {p.rel.alias("l")} JOIN {q(self.dimensions[g.over])} AS r ON l.{q(g.over)} = r.val',
            (*keep, g.into, *p.carried),
        )
        keyed = p.keyed and g.over in p.label_dims
        return TermFragment((*keep, g.into), rel, p.is_term, keyed, _relabel(p.label_dims, g.over, g.into))

    def _translate_fragment(self, p: TermFragment, s: plan.Translate, context: str) -> TermFragment:
        """A pointwise remap of the dim through its ord.

        Two joins on the dim table, never a window: that is what keeps this
        bounded-halo rather than global, and it is the property `#189`'s test
        suite asserts on the polars side (``OVER`` absent from a translation).
        """
        if s.dimension not in p.dims:
            raise LanguageError(
                f"in {context}: translation along '{s.dimension}' but the expression has dims {list(p.dims)}"
            )
        card = self.cardinality[s.dimension]
        others = [d for d in p.dims if d != s.dimension]
        table = q(self.dimensions[s.dimension])
        # SQL's % keeps the sign of its left operand, so a negative `by` would
        # land outside the table and simply fail to join — dropping the row
        # instead of wrapping it. The doubled modulo is not redundant.
        moved = f'(i.ord + {s.by})' if not s.wrap else f'((i.ord + {s.by}) % {card} + {card}) % {card}'

        def remap(rel: Rel, carried: Sequence[str]) -> Rel:
            picked = ''.join(f'v.{q(d)}, ' for d in others)
            extra = ''.join(f', v.{q(c)}' for c in carried)
            return Rel(
                f'SELECT {picked}o.val AS {q(s.dimension)}{extra} '
                f'FROM {rel.alias("v")} '
                f'JOIN {table} AS i ON v.{q(s.dimension)} = i.val '
                f'JOIN {table} AS o ON o.ord = {moved}',
                (*others, s.dimension, *carried),
            )

        rel = remap(p.rel, p.carried)
        if not s.wrap and s.fill:
            rel = Rel(
                f'{rel.sql} UNION ALL {self._filled_edge(s, card, others, s.fill).sql}',
                (*others, s.dimension, *p.carried),
            )
        presence, presence_dims = None, None
        if p.presence is not None:
            presence = remap(p.presence, ())
            if not s.wrap and s.fill is not None:
                vac = self._vacated(p, s, card, others)
                cols = ', '.join(q(c) for c in presence.columns)
                presence = Rel(
                    f'SELECT {cols} FROM ({presence.sql}) UNION SELECT {cols} FROM ({vac.sql})', presence.columns
                )
        elif not s.wrap and s.fill is None:
            presence, presence_dims = self._edge(s, card, vacated=False), (s.dimension,)
        return replace(p, rel=rel, presence=presence, presence_dims=presence_dims)

    def _filled_edge(self, s: plan.Translate, card: int, others: Sequence[str], fill: float) -> Rel:
        """``(dims…, cval=fill)`` at every coordinate the shift vacated."""
        edge = self._edge(s, card, vacated=True)
        picked = ''.join(f'o{i}.val AS {q(d)}, ' for i, d in enumerate(others))
        joins = ''.join(f' CROSS JOIN {q(self.dimensions[d])} AS o{i}' for i, d in enumerate(others))
        return Rel(
            f'SELECT {picked}e.{q(s.dimension)}, {lit(float(fill))} AS cval FROM ({edge.sql}) AS e{joins}',
            (*others, s.dimension, 'cval'),
        )

    def _edge(self, s: plan.Translate, card: int, *, vacated: bool) -> Rel:
        """The labels of ``s.dimension`` an acyclic shift vacates, or keeps.

        One predicate negated rather than two kept in step — a fill and the
        presence set it implies must not be able to disagree.
        """
        outside = f'(ord - {s.by} < 0 OR ord - {s.by} >= {card})'
        keep = outside if vacated else f'NOT {outside}'
        return Rel(
            f'SELECT val AS {q(s.dimension)} FROM {q(self.dimensions[s.dimension])} WHERE {keep}', (s.dimension,)
        )

    def _vacated(self, p: TermFragment, s: plan.Translate, card: int, others: Sequence[str]) -> Rel:
        """The edge positions ``shift`` leaves with nothing to move in."""
        edge = self._edge(s, card, vacated=True)
        if not others or p.presence is None:
            return edge
        picked = ', '.join(f'o.{q(d)}' for d in others)
        return Rel(
            f'SELECT {picked}, e.{q(s.dimension)} '
            f'FROM (SELECT DISTINCT {picked.replace("o.", "")} FROM {p.presence.alias("x")}) AS o '
            f'CROSS JOIN ({edge.sql}) AS e',
            (*others, s.dimension),
        )

    @staticmethod
    def constant_scalar(p: TermFragment) -> Rel:
        """The const fragment summed per coordinate: ``(dims…, cval)``."""
        if not p.dims:
            return Rel(f'SELECT sum(cval) AS cval FROM {p.rel.alias("c")}', ('cval',))
        keys = ', '.join(q(d) for d in p.dims)
        return Rel(f'SELECT {keys}, sum(cval) AS cval FROM {p.rel.alias("c")} GROUP BY {keys}', (*p.dims, 'cval'))


# --------------------------------------------------------------------------
# free functions — the polars module's, with SQL underneath
# --------------------------------------------------------------------------


def _relabel(label_dims: frozenset[str], over: str, into: str) -> frozenset[str]:
    return (label_dims - {over}) | {into} if over in label_dims else label_dims


def _compare(column: str, op: plan.ComparisonOperator, value: float | str) -> str:
    if op == '!=':
        return f'({column} IS DISTINCT FROM {lit(value)})'
    return f'({column} {op if op != "==" else "="} {lit(value)})'


def _negate(p: TermFragment) -> TermFragment:
    others = [q(c) for c in p.rel.columns if c != p.value_column]
    cols = ', '.join([*others, f'-{q(p.value_column)} AS {q(p.value_column)}'])
    return replace(p, rel=Rel(f'SELECT {cols} FROM {p.rel.alias("n")}', p.rel.columns))


def _join_mul(a: TermFragment, c: TermFragment, *, is_term: bool, divide: bool = False) -> TermFragment:
    """Broadcast-join two fragments and multiply (or divide) their values."""
    shared = [d for d in a.dims if d in c.dims]
    dims = (*a.dims, *(d for d in c.dims if d not in a.dims))
    value = 'coeff' if is_term else 'cval'
    op = '/' if divide else '*'
    left_cols = ''.join(f'l.{q(d)}, ' for d in a.dims)
    right_cols = ''.join(f'r.{q(d)}, ' for d in c.dims if d not in a.dims)
    label = 'l.var_label, ' if is_term else ''
    on = ' AND '.join(f'l.{q(d)} IS NOT DISTINCT FROM r.{q(d)}' for d in shared) or 'TRUE'
    # Left for a divide, so a coordinate the divisor has no value for yields a
    # *null* coefficient instead of silently dropping the term: the question is
    # not "is this divisor dense" but "is it defined where the model divides".
    join = ('LEFT JOIN' if divide else 'JOIN') if shared else 'CROSS JOIN'
    clause = f' ON {on}' if shared else ''
    sql = (
        f'SELECT {left_cols}{right_cols}{label}'
        f'l.{q(a.value_column)} {op} r.cval AS {q(value)} '
        f'FROM {a.rel.alias("l")} {join} {c.rel.alias("r")}{clause}'
    )
    return TermFragment(
        dims,
        Rel(sql, (*dims, *(('var_label',) if is_term else ()), value)),
        is_term,
        a.keyed,
        a.label_dims,
        a.presence,
        a.presence_dims,
    )


def _propagate_absence(compiled: CompiledExpression) -> CompiledExpression:
    """Restrict every fragment to where the *whole* expression exists.

    A reduction consumes the expression before any row exists, so without this
    each additive stream would be summed over its own coordinates —
    ``sum(x + size, over=f)`` silently becoming ``sum(x) + sum(size)``, which
    reads an absent ``size`` as zero (SPEC §6, §7).

    The semi-join is ``WHERE EXISTS``, which is the one place SQL says this
    more plainly than the dataframe does: polars needs `how='semi'` to mean
    "filter, do not widen", and a reader has to know that `semi` is not a join.
    """
    restrictions = [
        (p.presence_dims or p.dims, p.presence) for p in (*compiled.terms, *compiled.consts) if p.presence is not None
    ]
    if not restrictions:
        return _map_fragments(compiled, lambda p: replace(p, presence=None, presence_dims=None))

    def restrict(p: TermFragment) -> TermFragment:
        rel = p.rel
        for on, presence in restrictions:
            if all(d in p.dims for d in on):
                keep = ', '.join(f'l.{q(c)}' for c in rel.columns)
                names = ', '.join(q(d) for d in on)
                on_sql = ' AND '.join(f'l.{q(d)} IS NOT DISTINCT FROM r.{q(d)}' for d in on)
                rel = Rel(
                    f'SELECT {keep} FROM {rel.alias("l")} WHERE EXISTS '
                    f'(SELECT 1 FROM (SELECT DISTINCT {names} FROM {presence.alias("p")}) AS r WHERE {on_sql})',
                    rel.columns,
                )
        return replace(p, rel=rel, presence=None, presence_dims=None)

    return _map_fragments(compiled, restrict)


def _map_fragments(compiled: CompiledExpression, rewrite: Callable[[TermFragment], TermFragment]) -> CompiledExpression:
    """Apply *rewrite* to every fragment, keeping the term/const split."""
    return CompiledExpression(
        tuple(rewrite(p) for p in compiled.terms),
        tuple(rewrite(p) for p in compiled.consts),
    )
