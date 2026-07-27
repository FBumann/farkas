"""Logical plan → polars. Lazy: nothing is read, nothing is executed.

`lowering.py` compiles the AST to a plan; this compiles the plan to a polars
query. Two stages, two names, and this is the second one.

Nothing here runs anything. A :class:`PolarsCompiler` returns
:class:`polars.LazyFrame`\\ s — a declarative plan, not a result — which is
what makes the admissibility test in ARCHITECTURE.md ("read the verdict off
the plan") something you can perform: build a compiler, hand it a node, and
read ``.explain()``.

An identifier is a value here, never syntax. Nothing is quoted, nothing is
escaped, and the engine imposes no spelling rule on a declared name — a
dimension may be called ``from`` or ``order``.

The unit of output is a :class:`TermFragment`: one additive piece of an affine
expression, carried as a frame plus the dims it is indexed by. Compiling an
expression yields a term/const split, never a single frame — because an LP row
*is* a sum of pieces, and keeping them separate is what lets every shape
operator rewrite one piece at a time.

Column conventions, fixed here and relied on by the executor:

===================  ==========================================
frame                columns
===================  ==========================================
dimension table      ``val``, ``ord``, plus declared coordinates
parameter table      ``dims…``, ``value``
variable frame       ``dims…``, ``var_label``
term fragment        ``dims…``, ``var_label``, ``coeff``
const fragment       ``dims…``, ``cval``
===================  ==========================================
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from farkas.errors import LanguageError
from farkas.relational import plan

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import polars as pl

#: Scratch column for the right-hand side of a fragment multiply. Spaces make
#: it unrepresentable as a declared name, so it cannot collide with a dimension
#: or coordinate the model already has.
_RHS = '__rhs value__'

#: Scratch columns for a Translate: the ordinal a row sits at, and the one it
#: moves to. Same reasoning.
_ORD_IN = '__ord in__'
_ORD_OUT = '__ord out__'


@dataclass(frozen=True)
class TermFragment:
    """One additive piece of a compiled affine expression.

    ``frame`` is a full query. Term fragments yield ``(dims…, var_label,
    coeff)``; const fragments yield ``(dims…, cval)``.
    """

    dims: tuple[str, ...]
    frame: pl.LazyFrame
    is_term: bool

    keyed: bool = True
    """Whether the fragment holds at most one row per key.

    The key is ``(dims…, var_label)`` for a term and ``(dims…)`` for a constant
    part. Nothing about correctness depends on it — the assembly aggregates
    either way — but a *keyed* single-term expression cannot produce two rows
    for one ``(row, col)``, and that lets the executor skip an aggregate over
    every nonzero in the model. Tracked here rather than inferred there,
    because the only place that knows whether an operator can duplicate a row
    is the operator.

    ``GroupSum`` and ``Translate`` join a dim table one-to-one and always
    preserve it. ``Sum`` is the operator that can break it, and
    :attr:`label_dims` is what decides.
    """

    label_dims: frozenset[str] = frozenset()
    """The dims ``var_label`` determines — a variable's own ``foreach``.

    The rest of a term's dims got there by **broadcast**, from a parameter the
    variable was multiplied by, and the distinction is what makes ``Sum``
    safe or not. Dropping a dim the label determines merges rows that carry
    *different* labels, so the key survives. Dropping a broadcast dim merges
    rows carrying the *same* label, and the key does not.

    ``sum(q * price, over=generator)`` with ``q`` indexed by snapshot alone is
    the case: the fragment reduces to ``('snapshot',)`` — exactly ``q``'s
    declaration — while still holding one row per generator. A dims-equality
    test calls that keyed and is wrong.
    """

    @property
    def value_column(self) -> str:
        """``coeff`` for a term, ``cval`` for a constant part."""
        return 'coeff' if self.is_term else 'cval'

    @property
    def carried(self) -> list[str]:
        """The non-dim columns a projection has to keep."""
        return ['var_label', self.value_column] if self.is_term else [self.value_column]


@dataclass(frozen=True)
class CompiledExpression:
    """An affine expression as fragments: variable terms and a constant part."""

    terms: tuple[TermFragment, ...]
    consts: tuple[TermFragment, ...]


@dataclass(frozen=True)
class PolarsCompiler:
    """Turn plan nodes into polars queries over the model's tidy frames.

    ``dimension_cardinality`` and ``boolean_parameters`` are read off the data
    once it is loaded, which is one reason this is not a free function:
    ``sum`` over a dim the operand lacks scales by that dim's size, and
    ``defined`` on a boolean parameter tests the value rather than its
    finiteness.

    ``parameters`` / ``dimensions`` / ``variables`` are the executor's own
    dicts, read at compile time rather than copied at construction. A variable
    frame is created while its declaration is built, so a constraint compiled
    afterwards has to see a registry that has filled in since — holding the
    mapping rather than a snapshot of it is what allows that.
    """

    program: plan.Program
    dimension_cardinality: Mapping[str, int]
    boolean_parameters: frozenset[str]
    parameters: Mapping[str, pl.LazyFrame]
    dimensions: Mapping[str, pl.LazyFrame]
    variables: Mapping[str, pl.LazyFrame]

    # ------------------------------------------------------------------
    # frames — the masked coordinate product a declaration is instantiated over
    # ------------------------------------------------------------------

    def frame(self, dims: tuple[str, ...], where: plan.Predicate | None) -> pl.LazyFrame:
        """The masked coordinate product over *dims*.

        Columns are ``(dims…, <dim>_ord…)``: the labels, and the ordinals the
        caller orders by so that labels follow declaration order.

        One frame rather than the three pieces a caller would have to compose
        itself: the mask belongs to the coordinate product, not to whoever
        happens to use it next.
        """
        out = self._coordinate_product(dims)
        if where is None:
            return out
        out, condition = self._predicate(out, where, dims)
        # a NULL comparison (a missing parameter row) must exclude the row
        # rather than yield NULL, so the filter stays strictly boolean
        return out.filter(_falsy_if_null(condition))

    def _coordinate_product(self, dims: tuple[str, ...]) -> pl.LazyFrame:
        """Cross join of the dim tables: labels and ordinals, nothing else."""
        import polars as pl

        out: pl.LazyFrame | None = None
        for d in dims:
            table = self.dimensions[d].select(
                pl.col('val').alias(d),
                pl.col('ord').alias(_ordinal(d)),
            )
            out = table if out is None else out.join(table, how='cross')
        assert out is not None, 'a declaration with no dims is rejected before it reaches the compiler'
        return out

    def parameter_join(
        self,
        frame: pl.LazyFrame,
        param: str,
        frame_dims: tuple[str, ...],
        alias: str,
        subject: str,
    ) -> pl.LazyFrame:
        """Left-join *param* onto *frame*, its value column renamed to *alias*.

        Both callers — where-masks and variable bounds — need the same join and
        the same containment check: a parameter carrying a dim the frame does
        not have would be reduced over that dim, silently widening a mask or
        picking an arbitrary bound. They differ only in what the message calls
        the offending parameter (*subject*) — naming the declaration it came
        from is most of the value of raising here, so it is the caller's word,
        not a role prefix pasted on the front.
        """
        declaration = self.program.parameter(param)
        extra = set(declaration.dims) - set(frame_dims)
        if extra:
            raise LanguageError(f'{subject} has dims {sorted(extra)} outside the foreach dims {list(frame_dims)}')
        table = self.parameters[param].rename({'value': alias})
        if not declaration.dims:
            return frame.join(table, how='cross')
        return frame.join(table, on=list(declaration.dims), how='left')

    # ------------------------------------------------------------------
    # predicates (where masks — row absence)
    # ------------------------------------------------------------------

    def _predicate(
        self, frame: pl.LazyFrame, pred: plan.Predicate, dims: tuple[str, ...]
    ) -> tuple[pl.LazyFrame, pl.Expr]:
        """``(frame with the mask's parameters joined, boolean expression)``."""
        import polars as pl

        joined: set[str] = set()
        carrier = frame

        def join_param(param: str) -> str:
            nonlocal carrier
            alias = f'__where {param}__'
            if alias not in joined:
                carrier = self.parameter_join(carrier, param, dims, alias, f"where-parameter '{param}'")
                joined.add(alias)
            return alias

        def walk(p: plan.Predicate) -> pl.Expr:
            if isinstance(p, plan.ParameterComparison):
                return _compare(pl.col(join_param(p.parameter)), p.op, p.value)
            if isinstance(p, plan.DimensionComparison):
                if p.dimension not in dims:
                    raise LanguageError(
                        f"where-comparison on dimension '{p.dimension}' is outside the foreach dims "
                        f'{list(dims)} — reducing a mask over an unlisted dim is not supported'
                    )
                return _compare(pl.col(p.dimension), p.op, p.value)
            if isinstance(p, plan.ParameterDefined):
                col = pl.col(join_param(p.parameter))
                if p.parameter in self.boolean_parameters:
                    return col.is_not_null() & col.cast(pl.Boolean)
                return col.is_not_null() & col.is_finite()
            if isinstance(p, plan.BooleanConstant):
                return pl.lit(value=p.value)
            if isinstance(p, plan.And):
                return walk(p.left) & walk(p.right)
            if isinstance(p, plan.Or):
                return walk(p.left) | walk(p.right)
            if isinstance(p, plan.Not):
                return ~_falsy_if_null(walk(p.operand))
            raise LanguageError(f'unsupported predicate node {type(p).__name__}')

        condition = walk(pred)
        return carrier, condition

    # ------------------------------------------------------------------
    # bounds
    # ------------------------------------------------------------------

    def bounds(self, frame: pl.LazyFrame, v: plan.VariableDeclaration) -> pl.LazyFrame:
        """*frame* with ``lb``/``ub`` columns for variable *v*.

        The joins a bound needs and the arithmetic over them are one object,
        so a bound expression cannot be evaluated against a frame that is
        missing the parameters it reads.
        """
        import polars as pl

        carrier = frame
        joined: set[str] = set()

        def walk(e: plan.Expression) -> pl.Expr:
            nonlocal carrier
            if isinstance(e, plan.Constant):
                return pl.lit(float(e.value), dtype=pl.Float64)
            if isinstance(e, plan.Parameter):
                alias = f'__bound {e.name}__'
                if alias not in joined:
                    carrier = self.parameter_join(
                        carrier, e.name, v.dims, alias, f"bound parameter '{e.name}' of variable '{v.name}'"
                    )
                    joined.add(alias)
                return pl.col(alias).cast(pl.Float64)
            if isinstance(e, plan.Negate):
                return -walk(e.operand)
            if isinstance(e, plan.Add):
                return walk(e.left) + walk(e.right)
            if isinstance(e, plan.Multiply):
                return walk(e.left) * walk(e.right)
            raise LanguageError(
                f"unsupported node {type(e).__name__} in bounds of variable '{v.name}' "
                f'(bounds must be variable-free arithmetic over Constant/Parameter)'
            )

        lower, upper = walk(v.lower), walk(v.upper)
        return carrier.with_columns(lower.alias('lb'), upper.alias('ub'))

    # ------------------------------------------------------------------
    # expressions → fragments
    # ------------------------------------------------------------------

    def expression(self, expr: plan.Expression, context: str) -> CompiledExpression:
        """Compile an affine expression into term and const fragments."""
        import polars as pl

        def ev(e: plan.Expression) -> CompiledExpression:
            if isinstance(e, plan.Constant):
                frame = pl.LazyFrame({'cval': [float(e.value)]}, schema={'cval': pl.Float64})
                return CompiledExpression((), (TermFragment((), frame, False),))
            if isinstance(e, plan.Parameter):
                # keyed: a parameter is a function of its dims, and the
                # executor rejects a source that carries a coordinate twice
                d = self.program.parameter(e.name).dims
                frame = self.parameters[e.name].select(*d, pl.col('value').cast(pl.Float64).alias('cval'))
                return CompiledExpression((), (TermFragment(d, frame, False),))
            if isinstance(e, plan.Variable):
                d = self.program.variable(e.name).dims
                frame = self.variables[e.name].select(*d, 'var_label', pl.lit(1.0, dtype=pl.Float64).alias('coeff'))
                return CompiledExpression((TermFragment(d, frame, True, label_dims=frozenset(d)),), ())
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
                terms = tuple(_join_mul(t, inv, is_term=True, divide=True) for t in a.terms)
                consts = tuple(_join_mul(x, inv, is_term=False, divide=True) for x in a.consts)
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
        """Drop the summed dims. **Not an aggregate.**

        Dropping a coordinate column leaves the rows that carried it; the
        duplicates collapse in the terminal ``sum(coeff)`` grouped by
        ``(row, col)`` at assembly. Rewriting a fragment's dim tuple on its own
        is what pointwise locality means in code.
        """
        import polars as pl

        missing = [d for d in over if d not in p.dims]
        if missing and not p.is_term:
            raise LanguageError(
                f'in {context}: Sum over {list(over)} of a constant part lacking dims '
                f'{missing} is ambiguous under masks — multiply explicitly instead'
            )
        keep = tuple(d for d in p.dims if d not in over)
        scale = math.prod(self.dimension_cardinality[d] for d in missing)
        frame = p.frame.select(*keep, *p.carried)
        if scale != 1:
            frame = frame.with_columns(pl.col(p.value_column) * scale)
        # dropping a dim the label determines merges rows carrying *different*
        # labels, so the key survives; dropping a broadcast dim merges rows
        # carrying the same one, and it does not
        dropped = {d for d in p.dims if d not in keep}
        keyed = p.keyed and dropped <= p.label_dims
        return TermFragment(keep, frame, p.is_term, keyed, p.label_dims - dropped)

    def _group_fragment(self, p: TermFragment, g: plan.GroupSum, context: str) -> TermFragment:
        """Relabel dim ``over`` to ``into`` through a declared coordinate.

        No aggregate here either: the fragment's dim tuple changes and
        duplicate (row, col) pairs collapse at assembly — the same shape as
        :meth:`_sum_fragment` dropping a dim. The join is against the dim
        table, whose coordinate column was checked for containment at build
        time, so it cannot silently drop a term.
        """
        import polars as pl

        if g.over not in p.dims:
            raise LanguageError(f"in {context}: GroupSum over '{g.over}' but the expression has dims {list(p.dims)}")
        keep = tuple(x for x in p.dims if x != g.over)
        mapping = self.dimensions[g.over].select(
            pl.col('val').alias(g.over),
            pl.col(g.coordinate).alias(g.into),
        )
        # one row per label in the dim table, so this cannot duplicate a row.
        # The coordinate it projects is determined by the label exactly when
        # the dim it replaces was.
        labelled = p.label_dims - {g.over} | ({g.into} if g.over in p.label_dims else frozenset())
        frame = p.frame.join(mapping, on=g.over, how='inner').select(*keep, g.into, *p.carried)
        return TermFragment((*keep, g.into), frame, p.is_term, p.keyed, labelled)

    def _translate_fragment(self, p: TermFragment, s: plan.Translate, context: str) -> TermFragment:
        """Translation = a pointwise remap of the dim through its ord:
        a row at ord *o* contributes to the output coord at ord ``(o + by) %
        card``. No window function involved — bounded-halo locality."""
        import polars as pl

        if s.dimension not in p.dims:
            raise LanguageError(
                f"in {context}: translation along '{s.dimension}' but the expression has dims {list(p.dims)}"
            )
        card = self.dimension_cardinality[s.dimension]
        others = [d for d in p.dims if d != s.dimension]
        table = self.dimensions[s.dimension]
        incoming = table.select(pl.col('val').alias(s.dimension), pl.col('ord').alias(_ORD_IN))
        outgoing = table.select(pl.col('val').alias(s.dimension), pl.col('ord').alias(_ORD_OUT))

        moved = pl.col(_ORD_IN) + s.by
        if s.wrap:
            moved = (moved % card + card) % card
        frame = (
            p.frame.join(incoming, on=s.dimension, how='inner')
            .drop(s.dimension)
            .with_columns(moved.alias(_ORD_OUT))
            # acyclic: an out-of-range ordinal simply does not join, which is
            # the zero contribution the language promises
            .join(outgoing, on=_ORD_OUT, how='inner')
            .select(*others, s.dimension, *p.carried)
        )
        # both joins are on a dim table key, so the row count is unchanged
        return TermFragment(p.dims, frame, p.is_term, p.keyed, p.label_dims)

    # ------------------------------------------------------------------
    # assembly helpers used by the executor
    # ------------------------------------------------------------------

    @staticmethod
    def constant_scalar(p: TermFragment) -> pl.LazyFrame:
        """The const fragment summed per coordinate: ``(dims…, cval)``.

        Aggregated here and joined by the caller, which is one hash group-by
        for the whole fragment rather than a lookup repeated per frame row.
        """
        import polars as pl

        if not p.dims:
            return p.frame.select(pl.col('cval').sum())
        return p.frame.group_by(p.dims).agg(pl.col('cval').sum())


def _ordinal(dim: str) -> str:
    """The frame column carrying *dim*'s position in its declared order."""
    return f'__ord {dim}__'


def _falsy_if_null(condition: pl.Expr) -> pl.Expr:
    """``condition``, with null read as false.

    A missing parameter row makes a comparison null, and null must *exclude*
    the coordinate rather than propagate — masks are row absence.
    """
    return condition.fill_null(value=False)


def _compare(column: pl.Expr, op: plan.ComparisonOperator, value: float | str) -> pl.Expr:
    """One where-comparison.

    The language's ``==`` is polars' ``eq``. There is no branch on the value's
    type: a string and a float are both literals, never spelled differently.
    """
    import polars as pl

    literal = pl.lit(value)
    match op:
        case '==':
            return column == literal
        case '!=':
            return column != literal
        case '<':
            return column < literal
        case '<=':
            return column <= literal
        case '>':
            return column > literal
        case '>=':
            return column >= literal


def _map_fragments(
    compiled: CompiledExpression,
    rewrite: Callable[[TermFragment], TermFragment],
) -> CompiledExpression:
    """Apply *rewrite* to every fragment, keeping the term/const split.

    Sum, GroupSum and Translate all rewrite each fragment on its own, which is
    what pointwise and bounded-halo locality mean in code (ARCHITECTURE.md,
    "Read the verdict off the plan"). A node that needed the fragments
    *together* would be a global operator, rejected at lowering instead.
    """
    return CompiledExpression(
        tuple(rewrite(p) for p in compiled.terms),
        tuple(rewrite(p) for p in compiled.consts),
    )


def _negate(p: TermFragment) -> TermFragment:
    import polars as pl

    return TermFragment(p.dims, p.frame.with_columns(-pl.col(p.value_column)), p.is_term, p.keyed, p.label_dims)


def _join_mul(a: TermFragment, c: TermFragment, is_term: bool, divide: bool = False) -> TermFragment:
    """``a * c`` (or ``a / c``) where *c* is a const fragment.

    Joins on shared dims and broadcasts the rest, which is a cross join. The
    right-hand value is renamed out of the way first: both fragments may carry
    ``cval``, and letting polars resolve that collision with a suffix would
    silently multiply a column by itself.
    """
    import polars as pl

    shared = [d for d in a.dims if d in c.dims]
    out_dims = a.dims + tuple(d for d in c.dims if d not in a.dims)
    right = c.frame.rename({'cval': _RHS})
    joined = a.frame.join(right, on=shared, how='inner') if shared else a.frame.join(right, how='cross')

    value, rhs = pl.col(a.value_column), pl.col(_RHS)
    combined = value / rhs if divide else value * rhs
    out = 'coeff' if is_term else 'cval'
    carried = ['var_label', out] if is_term else [out]
    frame = joined.with_columns(combined.alias(out)).select(*out_dims, *carried)
    # the dims c contributes are broadcast: the label says nothing about them
    return TermFragment(out_dims, frame, is_term, a.keyed and c.keyed, a.label_dims)
