"""Logical plan → SQL. Pure: no connection, no execution, no I/O.

`lowering.py` compiles the AST to a plan; this compiles the plan to SQL text.
Two stages, two names, and this is the second one.

Nothing here runs anything. A :class:`SqlCompiler` needs three facts about the
model — the program, each dimension's cardinality, and which parameters are
boolean-valued — and from those it returns strings. That is what makes the
admissibility test in ARCHITECTURE.md ("read the verdict off the SQL")
something you can actually do: build a compiler, hand it a plan node, read the
SELECT it produces, and decide whether the operator is pointwise, bounded-halo
or global. No duckdb instance required, which is also why
``tests/test_compiler.py`` runs on a bare install.

The unit of output is a :class:`TermFragment`: one additive piece of an affine
expression, carried as a full SELECT plus the dims it is indexed by. Compiling
an expression yields a term/const split, never a single query — because an LP
row *is* a sum of pieces, and keeping them separate is what lets every shape
operator rewrite one piece at a time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from farkas.errors import LanguageError
from farkas.relational import plan

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


@dataclass(frozen=True)
class TermFragment:
    """One additive piece of a compiled affine expression.

    ``sql`` is a full SELECT. Term fragments yield ``(dims…, var_label,
    coeff)``; const fragments yield ``(dims…, cval)``.
    """

    dims: tuple[str, ...]
    sql: str
    is_term: bool

    presence: str | None = None
    """A SELECT of the coordinates where the *variable* under this fragment exists.

    Not the same question as which rows :attr:`sql` returns. A fragment loses
    rows for two unrelated reasons and a constraint row must react to only one:
    a **masked variable** is genuinely absent there, while a **sparse parameter**
    is a compressed dense array whose missing rows mean a zero coefficient
    (SPEC §8). Once the two are joined the result cannot tell them apart, so the
    variable's own coordinates travel alongside.

    ``None`` means nothing to report — a constant has no variable, an *unmasked*
    variable exists everywhere, and a reduction clears it because ``sum`` skips
    absent slots rather than propagating them.
    """


@dataclass(frozen=True)
class CompiledExpression:
    """An affine expression as fragments: variable terms and a constant part."""

    terms: tuple[TermFragment, ...]
    consts: tuple[TermFragment, ...]


@dataclass(frozen=True)
class SqlCompiler:
    """Turn plan nodes into SQL over the model's tidy tables.

    ``dimension_cardinality`` and ``boolean_parameters`` are read off the data
    once it is loaded, which is the only reason this is not a free function:
    ``sum`` over a dim the operand lacks scales by that dim's size, and
    ``defined`` on a boolean parameter tests the value rather than its
    finiteness.
    """

    program: plan.Program
    dimension_cardinality: Mapping[str, int]
    boolean_parameters: frozenset[str]

    # ------------------------------------------------------------------
    # frames — the masked coordinate product a declaration is instantiated over
    # ------------------------------------------------------------------

    def frame(self, dims: tuple[str, ...], where: plan.Predicate | None) -> tuple[str, str, str]:
        """FROM/WHERE clauses of the (masked) coord product and its order key.

        Returns ``(from_clause, where_clause, order_key)``; the select list can
        project ``t_<dim>.val AS <dim>``.
        """
        froms = [f'dim_{dims[0]} t_{dims[0]}']
        froms += [f'CROSS JOIN dim_{d} t_{d}' for d in dims[1:]]

        conds: list[str] = []
        if where is not None:
            joins, cond = self.predicate(where, dims)
            froms += joins
            conds.append(cond)
        return ' '.join(froms), (' AND '.join(conds) if conds else 'TRUE'), ', '.join(f't_{d}.ord' for d in dims)

    def positional_label(self, dims: tuple[str, ...], start: int) -> str:
        """The dense label of an *unmasked* frame row, as arithmetic on ordinals.

        Labels are row-major positions in the coordinate product, so when no
        mask removes anything the position is computable rather than countable:
        ``start + Σ ord_d · stride_d``, the strides being the products of the
        trailing dims' cardinalities. Same numbers a ``ROW_NUMBER`` over
        :meth:`frame`'s ``order_key`` would assign — which is the point, since
        a variable's label *is* its solver column index and the two paths must
        agree on it exactly.

        Only valid without a mask. Under one the surviving rows are not known
        until the predicate has run, and counting them is what the window is
        for; :meth:`frame`'s ``order_key`` stays the definition of the order.
        """
        strides: list[int] = []
        stride = 1
        for d in reversed(dims):
            strides.append(stride)
            stride *= self.dimension_cardinality[d]
        parts = [f't_{d}.ord' if s == 1 else f't_{d}.ord * {s}' for d, s in zip(dims, reversed(strides), strict=True)]
        if start:
            parts.append(str(start))
        return f'({" + ".join(parts)})::BIGINT'

    def parameter_join(
        self,
        param: str,
        frame_dims: tuple[str, ...],
        alias: str,
        coordinate: str,
        subject: str,
    ) -> str:
        """The ``LEFT JOIN`` clause binding *param* to the frame under *alias*.

        Both callers — where-masks and variable bounds — need the same join and
        the same containment check: a parameter carrying a dim the frame does
        not have would be reduced over that dim, silently widening a mask or
        picking an arbitrary bound. They differ only in how the frame spells a
        coordinate column (*coordinate*, a ``{dim}`` template) and in what the
        message calls the offending parameter (*subject*) — naming the
        declaration it came from is most of the value of raising here, so it is
        the caller's word, not a role prefix pasted on the front.
        """
        declaration = self.program.parameter(param)
        extra = set(declaration.dims) - set(frame_dims)
        if extra:
            raise LanguageError(f'{subject} has dims {sorted(extra)} outside the foreach dims {list(frame_dims)}')
        on = ' AND '.join(f'{alias}.{d} = {coordinate.format(dim=d)}' for d in declaration.dims) or 'TRUE'
        return f'LEFT JOIN p_{param} {alias} ON {on}'

    # ------------------------------------------------------------------
    # predicates (where masks — row absence)
    # ------------------------------------------------------------------

    def predicate(self, pred: plan.Predicate, dims: tuple[str, ...]) -> tuple[list[str], str]:
        """``(join clauses, boolean condition)`` for a where-mask over *dims*."""
        joins: dict[str, str] = {}

        def join_param(param: str) -> str:
            alias = f'w_{param}'
            joins[alias] = self.parameter_join(param, dims, alias, 't_{dim}.val', f"where-parameter '{param}'")
            return alias

        def walk(p: plan.Predicate) -> str:
            if isinstance(p, plan.ParameterComparison):
                return _comparison_sql(f'{join_param(p.parameter)}.value', p.op, p.value)
            if isinstance(p, plan.DimensionComparison):
                if p.dimension not in dims:
                    raise LanguageError(
                        f"where-comparison on dimension '{p.dimension}' is outside the foreach dims "
                        f'{list(dims)} — reducing a mask over an unlisted dim is not supported'
                    )
                return _comparison_sql(f't_{p.dimension}.val', p.op, p.value)
            if isinstance(p, plan.ParameterDefined):
                alias = join_param(p.parameter)
                if p.parameter in self.boolean_parameters:
                    return f'({alias}.value IS NOT NULL AND {alias}.value)'
                return f'({alias}.value IS NOT NULL AND isfinite({alias}.value))'
            if isinstance(p, plan.VariableDefined):
                # Existence lives in the variable's own table, so it is a
                # semi-join marked with a flag: LEFT JOIN and test for a hit.
                # The dim rule has already checked the variable's dims are
                # inside this frame.
                alias = f'wv_{p.variable}'
                on = ' AND '.join(f'{alias}.{d} = t_{d}.val' for d in self.program.variable(p.variable).dims)
                joins[alias] = f'LEFT JOIN var_{p.variable} {alias} ON {on}'
                return f'({alias}.var_label IS NOT NULL)'
            if isinstance(p, plan.BooleanConstant):
                return 'TRUE' if p.value else 'FALSE'
            if isinstance(p, (plan.And, plan.Or)):
                op = 'AND' if isinstance(p, plan.And) else 'OR'
                return f'({walk(p.left)} {op} {walk(p.right)})'
            if isinstance(p, plan.Not):
                return f'(NOT COALESCE({walk(p.operand)}, FALSE))'
            raise LanguageError(f'unsupported predicate node {type(p).__name__}')

        cond = walk(pred)
        # NULL comparisons (missing parameter rows) must exclude the row, not
        # yield NULL — wrap so the frame filter is strictly boolean.
        return list(joins.values()), f'COALESCE({cond}, FALSE)'

    # ------------------------------------------------------------------
    # bounds
    # ------------------------------------------------------------------

    def bound(self, expr: plan.Expression, v: plan.VariableDeclaration) -> tuple[str, list[str]]:
        """Compile a variable-free bound expression to a scalar SQL expression
        over alias ``f`` (the variable frame), returning (sql, join clauses)."""
        joins: dict[str, str] = {}

        def walk(e: plan.Expression) -> str:
            if isinstance(e, plan.Constant):
                return _literal(e.value)
            if isinstance(e, plan.Parameter):
                alias = f'b_{e.name}'
                joins[alias] = self.parameter_join(
                    e.name, v.dims, alias, 'f.{dim}', f"bound parameter '{e.name}' of variable '{v.name}'"
                )
                return f'{alias}.value'
            if isinstance(e, plan.Negate):
                return f'(-({walk(e.operand)}))'
            if isinstance(e, plan.Add):
                return f'({walk(e.left)} + {walk(e.right)})'
            if isinstance(e, plan.Multiply):
                return f'({walk(e.left)} * {walk(e.right)})'
            raise LanguageError(
                f"unsupported node {type(e).__name__} in bounds of variable '{v.name}' "
                f'(bounds must be variable-free arithmetic over Constant/Parameter)'
            )

        return walk(expr), list(joins.values())

    # ------------------------------------------------------------------
    # expressions → fragments
    # ------------------------------------------------------------------

    def expression(self, expr: plan.Expression, context: str) -> CompiledExpression:
        """Compile an affine expression into term and const fragments."""

        def ev(e: plan.Expression) -> CompiledExpression:
            if isinstance(e, plan.Constant):
                return CompiledExpression((), (TermFragment((), f'SELECT {_literal(e.value)} AS cval', False),))
            if isinstance(e, plan.Parameter):
                d = self.program.parameter(e.name).dims
                cols = ', '.join([*d, 'value AS cval']) if d else 'value AS cval'
                return CompiledExpression((), (TermFragment(d, f'SELECT {cols} FROM p_{e.name}', False),))
            if isinstance(e, plan.Variable):
                d = self.program.variable(e.name).dims
                cols = ', '.join([*d, 'var_label', '1.0 AS coeff'])
                # Only a *masked* variable can restrict anything, and whether it
                # is masked is on the declaration — decided before data is read,
                # so an unmasked one never costs the label planner its fast path.
                presence = (
                    f'SELECT {", ".join(d)} FROM var_{e.name}'
                    if self.program.variable(e.name).where is not None and d
                    else None
                )
                return CompiledExpression((TermFragment(d, f'SELECT {cols} FROM var_{e.name}', True, presence),), ())
            if isinstance(e, plan.Negate):
                return _map_fragments(ev(e.operand), _negate)
            if isinstance(e, plan.Add):
                a, b = ev(e.left), ev(e.right)
                return CompiledExpression(a.terms + b.terms, a.consts + b.consts)
            if isinstance(e, plan.Multiply):
                a, b = ev(e.left), ev(e.right)
                if a.terms and b.terms:
                    raise LanguageError(f'nonlinear product in {context}: both factors contain variables')
                if b.terms:  # normalise: terms on the left
                    a, b = b, a
                terms = tuple(_join_mul(t, c, is_term=True) for t in a.terms for c in b.consts)
                consts = tuple(_join_mul(x, c, is_term=False) for x in a.consts for c in b.consts)
                return CompiledExpression(terms, consts)
            if isinstance(e, plan.Divide):
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
                return CompiledExpression(terms, consts)
            if isinstance(e, plan.Sum):
                return _map_fragments(ev(e.operand), lambda p: self._sum_fragment(p, e.over, context))
            if isinstance(e, plan.GroupSum):
                return _map_fragments(ev(e.operand), lambda p: self._group_fragment(p, e, context))
            if isinstance(e, plan.Translate):
                return _map_fragments(ev(e.operand), lambda p: self._translate_fragment(p, e, context))
            raise LanguageError(f'unsupported expression node {type(e).__name__} in {context}')

        return ev(expr)

    # ------------------------------------------------------------------
    # shape operators — one dim rewritten per fragment
    # ------------------------------------------------------------------

    def _sum_fragment(self, p: TermFragment, over: tuple[str, ...], context: str) -> TermFragment:
        missing = [d for d in over if d not in p.dims]
        if missing and not p.is_term:
            raise LanguageError(
                f'in {context}: Sum over {list(over)} of a constant part lacking dims '
                f'{missing} is ambiguous under masks — multiply explicitly instead'
            )
        keep = tuple(d for d in p.dims if d not in over)
        scale = math.prod(self.dimension_cardinality[d] for d in missing)
        valcols = 'var_label, coeff' if p.is_term else 'cval'
        if scale != 1:
            valcols = f'var_label, coeff * {scale} AS coeff' if p.is_term else f'cval * {scale} AS cval'
        cols = ', '.join([*keep, valcols]) if keep else valcols
        return TermFragment(keep, f'SELECT {cols} FROM ({p.sql})', p.is_term)

    def _group_fragment(self, p: TermFragment, g: plan.GroupSum, context: str) -> TermFragment:
        """Relabel dim ``over`` to ``into`` through a declared coordinate.

        No aggregate here: the fragment's dim tuple changes and duplicate
        (row, col) pairs collapse in the terminal ``SUM(coeff)`` at assembly —
        the same shape as :meth:`_sum_fragment` dropping a dim. The join is
        against the dim table, whose coordinate column was checked for
        containment at build time, so it cannot silently drop a term.
        """
        if g.over not in p.dims:
            raise LanguageError(f"in {context}: GroupSum over '{g.over}' but the expression has dims {list(p.dims)}")
        keep = tuple(x for x in p.dims if x != g.over)
        valcols = 't.var_label, t.coeff' if p.is_term else 't.cval'
        keepcols = ', '.join([*(f't.{x}' for x in keep), f'g."{g.coordinate}" AS {g.into}', valcols])
        sql = f'SELECT {keepcols} FROM ({p.sql}) t JOIN dim_{g.over} g ON g.val = t.{g.over}'
        return TermFragment((*keep, g.into), sql, p.is_term)

    def _translate_fragment(self, p: TermFragment, s: plan.Translate, context: str) -> TermFragment:
        """Translation = a pointwise remap of the dim through its ord:
        a row at ord *o* contributes to the output coord at ord ``(o + by) %
        card``. No window function involved — bounded-halo locality."""
        if s.dimension not in p.dims:
            raise LanguageError(
                f"in {context}: translation along '{s.dimension}' but the expression has dims {list(p.dims)}"
            )
        card = self.dimension_cardinality[s.dimension]
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
        presence = None
        if p.presence is not None:
            pcols = ', '.join([*(f't.{d}' for d in others), f'd_out.val AS {s.dimension}'])
            presence = (
                f'SELECT {pcols} FROM ({p.presence}) t '
                f'JOIN dim_{s.dimension} d_in ON d_in.val = t.{s.dimension} '
                f'JOIN dim_{s.dimension} d_out ON {on}'
            )
            if not s.wrap:
                # The edge `shift` leaves empty is *not* absent: SPEC §7 fixes what
                # it contributes ("vacated positions contribute zero"), a declared
                # rule of the language, so the row survives there. Only the
                # variable's own mask removes a coordinate, and the remap above
                # already dropped those.
                edge = (
                    f'SELECT val AS {s.dimension} FROM dim_{s.dimension} '
                    f'WHERE ord - {s.by} < 0 OR ord - {s.by} >= {card}'
                )
                if others:
                    ocols = ', '.join(f'o.{d}' for d in others)
                    edge = (
                        f'SELECT {ocols}, e.{s.dimension} FROM '
                        f'(SELECT DISTINCT {", ".join(others)} FROM ({p.presence})) o, ({edge}) e'
                    )
                presence = f'SELECT * FROM ({presence}) UNION ({edge})'
        return TermFragment(p.dims, sql, p.is_term, presence)

    # ------------------------------------------------------------------
    # assembly helpers used by the executor
    # ------------------------------------------------------------------

    @staticmethod
    def constant_scalar(p: TermFragment) -> str:
        """Correlated scalar: the summed const fragment value for frame row ``f``."""
        cond = ' AND '.join(f'q.{d} = f.{d}' for d in p.dims) or 'TRUE'
        return f'SELECT SUM(q.cval) FROM ({p.sql}) q WHERE {cond}'


def _literal(v: float) -> str:
    if math.isinf(v):
        return "('infinity'::DOUBLE)" if v > 0 else "('-infinity'::DOUBLE)"
    # _literal(0) type-checks (int -> float) and would emit '0': INTEGER, not DOUBLE
    # pyrefly: ignore[unnecessary-type-conversion]
    return repr(float(v))


def _map_fragments(
    compiled: CompiledExpression,
    rewrite: Callable[[TermFragment], TermFragment],
) -> CompiledExpression:
    """Apply *rewrite* to every fragment, keeping the term/const split.

    Sum, GroupSum and Translate all rewrite each fragment on its own, which is
    what pointwise and bounded-halo locality mean in code (ARCHITECTURE.md,
    "Read the verdict off the SQL"). A node that needed the fragments
    *together* would be a global operator, rejected at lowering instead.
    """
    return CompiledExpression(
        tuple(rewrite(p) for p in compiled.terms),
        tuple(rewrite(p) for p in compiled.consts),
    )


def _comparison_sql(column: str, op: plan.ComparisonOperator, value: float | str) -> str:
    """One where-comparison: ``(<column> <op> <literal>)``.

    The language's ``==`` is SQL's ``=``, and a string literal needs quoting —
    stated once, since the parameter and dimension cases differ only in which
    column they test.
    """
    literal = f"'{value}'" if isinstance(value, str) else repr(value)
    return f'({column} {"=" if op == "==" else op} {literal})'


def _negate(p: TermFragment) -> TermFragment:
    cols = 'var_label, -coeff AS coeff' if p.is_term else '-cval AS cval'
    sel = ', '.join([*p.dims, cols]) if p.dims else cols
    return TermFragment(p.dims, f'SELECT {sel} FROM ({p.sql})', p.is_term, p.presence)


def _join_mul(a: TermFragment, c: TermFragment, is_term: bool, op: str = '*') -> TermFragment:
    """a op c where ``c`` is a const fragment; join on shared dims, broadcast the rest."""
    shared = [d for d in a.dims if d in c.dims]
    on = ' AND '.join(f'a.{d} = c.{d}' for d in shared) or 'TRUE'
    out_dims = a.dims + tuple(d for d in c.dims if d not in a.dims)
    dimcols = [
        *(f'a.{d}' for d in a.dims),
        *(f'c.{d}' for d in c.dims if d not in a.dims),
    ]
    val = f'a.var_label, a.coeff {op} c.cval AS coeff' if is_term else f'a.cval {op} c.cval AS cval'
    sel = ', '.join([*dimcols, val])
    return TermFragment(
        out_dims,
        f'SELECT {sel} FROM ({a.sql}) a JOIN ({c.sql}) c ON {on}',
        is_term,
        # *c* is variable-free: a sparse coefficient zeroes a term, it does not
        # unmake the variable underneath it.
        a.presence,
    )
