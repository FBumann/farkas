"""Logical plan → polars. Lazy: nothing is read, nothing is executed.

`lowering.py` compiles the AST to a plan; this compiles the plan to a query, so
docs/ARCHITECTURE.md's admissibility test is a ``.explain()`` away. An identifier is
a value here, never syntax.

Column conventions, relied on by the executor:

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
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import polars as pl

from lpspec.errors import LanguageError
from lpspec.relational import plan

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


#: Scratch columns. The spaces make them unrepresentable as declared names, so
#: they cannot collide with a dimension or coordinate the model already has.
_RHS = '__rhs value__'
_ORD_IN = '__ord in__'
_ORD_OUT = '__ord out__'

#: Carries the single row of the empty coordinate product. Polars cannot hold a
#: frame with one row and no columns — collecting one reports ``(0, 0)`` — so the
#: unit needs a column to exist in, and every path drops it by selecting the
#: dims and the label instead.
UNIT = '__unit__'


@dataclass(frozen=True)
class TermFragment:
    """One additive piece of a compiled affine expression.

    Terms yield ``(dims…, var_label, coeff)``, const parts ``(dims…, cval)``.
    An LP row *is* a sum of pieces, so every shape operator rewrites one.
    """

    dims: tuple[str, ...]
    frame: pl.LazyFrame
    is_term: bool

    keyed: bool = True
    """At most one row per ``(dims…, var_label)``.

    Never needed for correctness — the assembly aggregates either way — but it
    lets the executor skip an aggregate over every nonzero in the model.
    """
    label_dims: frozenset[str] = frozenset()
    """The dims ``var_label`` determines: a variable's own ``foreach``.

    A term's other dims arrived by broadcast. See :meth:`survives_dropping`.
    """
    presence: pl.LazyFrame | None = None
    """Where the *variable* under this fragment exists, keyed by :attr:`dims`.

    Not the same question as which rows :attr:`frame` has. A fragment loses rows
    for two unrelated reasons, and a constraint row must react to only one of
    them: a **masked variable** is genuinely absent there, while a **sparse
    parameter** is a compressed dense array whose missing rows mean a zero
    coefficient (SPEC §8). Once the two are multiplied together the frame cannot
    tell them apart, so the variable's own coordinates are carried alongside.

    ``None`` means "nothing to report" — a constant fragment has no variable, and
    a reduction clears it, because ``sum`` skips absent slots rather than
    propagating them (v1 ``convention.rst`` §13).
    """
    presence_dims: tuple[str, ...] | None = None
    """The columns :attr:`presence` is keyed by; ``None`` means :attr:`dims`.

    Only an acyclic ``shift`` sets it. The coordinates it vacates are an edge
    along *one* dimension and say nothing about the others, so the restriction
    it introduces is one column wide however many dims the fragment carries —
    where keying it by :attr:`dims` would mean materialising the whole
    coordinate product just to name an edge.
    """

    @property
    def value_column(self) -> str:
        """``coeff`` for a term, ``cval`` for a constant part."""
        return 'coeff' if self.is_term else 'cval'

    @property
    def carried(self) -> list[str]:
        """The non-dim columns a projection has to keep."""
        return ['var_label', self.value_column] if self.is_term else [self.value_column]

    def survives_dropping(self, dropped: set[str]) -> bool:
        """Whether the key survives losing *dropped* from the dim tuple.

        Dropping a label dim merges rows with *different* labels, so the key
        holds; dropping a broadcast dim merges rows with the *same* one, and it
        does not. ``sum(q * price, over=generator)`` with ``q`` indexed by
        snapshot alone reduces to ``q``'s own dims while still holding a row
        per generator.
        """
        return self.keyed and dropped <= self.label_dims


@dataclass(frozen=True)
class CompiledExpression:
    """An affine expression as fragments: variable terms and a constant part."""

    terms: tuple[TermFragment, ...]
    consts: tuple[TermFragment, ...]


@dataclass(frozen=True)
class PolarsCompiler:
    """Turn plan nodes into polars queries over the model's tidy frames.

    ``dimension_cardinality`` and ``boolean_parameters`` are read off the data:
    ``sum`` over an absent dim scales by that dim's size, and ``defined`` on a
    boolean parameter tests the value rather than its finiteness.

    The frame registries are the executor's own dicts, not copies — a variable
    frame appears while its declaration is built, and a constraint compiled
    afterwards has to see it.
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
        """The masked coordinate product over *dims*: labels, plus the
        ordinals a caller sorts by so labels follow declaration order."""
        out = self._coordinate_product(dims)
        if where is None:
            return out
        out, condition = self._predicate(out, where, dims)
        return out.filter(_falsy_if_null(condition))

    def _coordinate_product(self, dims: tuple[str, ...]) -> pl.LazyFrame:
        """Cross join of the dim tables: labels and ordinals, nothing else."""

        out: pl.LazyFrame | None = None
        for d in dims:
            table = self.dimensions[d].select(pl.col('val').alias(d), pl.col('ord').alias(_ordinal(d)))
            out = table if out is None else out.join(table, how='cross')
        if out is None:
            # The empty cross join's unit is one row, not nothing — and the row
            # has to be real, since a `where` on a scalar declaration filters
            # this frame and nothing survives a filter.
            return pl.LazyFrame({UNIT: [0]})
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

        A parameter carrying a dim the frame lacks would be reduced over it,
        widening a mask or picking an arbitrary bound, so that is refused.
        *subject* is the caller's word for it, since naming the declaration is
        most of the value.
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
        """``(frame with the mask's parameters joined, boolean expression)``.

        Walking joins the parameters, so the condition is built first and the
        frame read after — one expression would return the pre-walk frame.
        """

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
            if isinstance(p, plan.VariableDefined):
                # Not a column test: existence lives in the variable's own frame,
                # so it is marked by a semi-join and then read as a flag. The join
                # is on the variable's dims, which the dim rule has already
                # checked are inside this frame.
                nonlocal carrier
                flag = f'__where defined {p.variable}__'
                if flag not in joined:
                    on = list(self.program.variable(p.variable).dims)
                    marked = (
                        self.variables[p.variable].select(*on).unique().with_columns(pl.lit(value=True).alias(flag))
                    )
                    carrier = carrier.join(marked, on=on, how='left')
                    joined.add(flag)
                return pl.col(flag).fill_null(value=False)
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

        Joins and arithmetic are one object, so a bound cannot be evaluated
        against a frame missing what it reads.
        """

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

        def ev(e: plan.Expression) -> CompiledExpression:
            if isinstance(e, plan.Constant):
                frame = pl.LazyFrame({'cval': [float(e.value)]}, schema={'cval': pl.Float64})
                return CompiledExpression((), (TermFragment((), frame, False),))
            if isinstance(e, plan.Parameter):
                return CompiledExpression((), (self._parameter_fragment(e.name),))
            if isinstance(e, plan.Variable):
                return CompiledExpression((self._variable_fragment(e.name),), ())
            if isinstance(e, plan.Negate):
                return _map_fragments(ev(e.operand), _negate)
            if isinstance(e, plan.Add):
                a, b = ev(e.left), ev(e.right)
                return CompiledExpression(a.terms + b.terms, a.consts + b.consts)
            if isinstance(e, plan.Multiply):
                return self._product(ev(e.left), ev(e.right), context)
            if isinstance(e, plan.Divide):
                return self._quotient(ev(e.numerator), ev(e.divisor), context)
            if isinstance(e, plan.Sum):
                inner = _propagate_absence(ev(e.operand))
                return _map_fragments(inner, lambda p: self._sum_fragment(p, e.over, context))
            if isinstance(e, plan.GroupSum):
                inner = _propagate_absence(ev(e.operand))
                return _map_fragments(inner, lambda p: self._group_fragment(p, e, context))
            if isinstance(e, plan.Translate):
                return _map_fragments(ev(e.operand), lambda p: self._translate_fragment(p, e, context))
            raise LanguageError(f'unsupported expression node {type(e).__name__} in {context}')

        return ev(expr)

    def _parameter_fragment(self, name: str) -> TermFragment:
        """A parameter as a constant part, keyed by its declared dims —
        which the executor enforces by refusing a duplicated coordinate."""

        dims = self.program.parameter(name).dims
        frame = self.parameters[name].select(*dims, pl.col('value').cast(pl.Float64).alias('cval'))
        return TermFragment(dims, frame, False)

    def _variable_fragment(self, name: str) -> TermFragment:
        """A variable as a term with unit coefficients."""

        dims = self.program.variable(name).dims
        frame = self.variables[name].select(*dims, 'var_label', pl.lit(1.0, dtype=pl.Float64).alias('coeff'))
        # An *unmasked* variable exists at every coordinate of its foreach, so
        # its presence could only ever restrict nothing. Leaving it None is not
        # an optimisation detail: a presence frame is data, and carrying one
        # costs `_label_frame` both of its arithmetic paths. Whether it is
        # needed is decided here, off the declaration, before any data is read.
        masked = self.program.variable(name).where is not None
        presence = self.variables[name].select(*dims) if masked else None
        # `presence_dims` is stated rather than left implied. It defaults to
        # None, meaning "keyed by dims" — but dims are rewritten downstream (a
        # product broadcasts, a sum drops) while this frame is not, so an
        # implied key silently becomes a claim about columns it never had.
        return TermFragment(
            dims,
            frame,
            True,
            label_dims=frozenset(dims),
            presence=presence,
            presence_dims=dims if masked else None,
        )

    def _product(self, a: CompiledExpression, b: CompiledExpression, context: str) -> CompiledExpression:
        """``a * b``, with the variable-carrying side normalised to the left."""
        if a.terms and b.terms:
            raise LanguageError(f'nonlinear product in {context}: both factors contain variables')
        if b.terms:
            a, b = b, a
        terms = tuple(_join_mul(t, c, is_term=True) for t in a.terms for c in b.consts)
        consts = tuple(_join_mul(x, c, is_term=False) for x in a.consts for c in b.consts)
        return CompiledExpression(terms, consts)

    def _quotient(self, a: CompiledExpression, b: CompiledExpression, context: str) -> CompiledExpression:
        """``a / b``, where *b* must be a single variable-free factor."""
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

    # ------------------------------------------------------------------
    # shape operators — one dim rewritten per fragment
    # ------------------------------------------------------------------

    def _sum_fragment(self, p: TermFragment, over: tuple[str, ...], context: str) -> TermFragment:
        """Drop the summed dims. **Not an aggregate.**

        The rows that carried them stay, and collapse in the terminal
        ``sum(coeff)`` at assembly.
        """

        missing = [d for d in over if d not in p.dims]
        if missing and not p.is_term:
            raise LanguageError(
                f'in {context}: Sum over {list(over)} of a constant part lacking dims '
                f'{missing} is ambiguous under masks — multiply explicitly instead'
            )
        keep = tuple(d for d in p.dims if d not in over)
        dropped = {d for d in p.dims if d not in keep}
        scale = math.prod(self.dimension_cardinality[d] for d in missing)
        frame = p.frame.select(*keep, *p.carried)
        if scale != 1:
            frame = frame.with_columns(pl.col(p.value_column) * scale)
        # §13: a reduction *skips* absent slots rather than propagating them, so
        # summing over a partly-masked dim is well defined and reports nothing.
        # Constructed rather than `replace`d for exactly that: `presence` has to
        # be *dropped* here, and carrying it would be the silent default.
        return TermFragment(keep, frame, p.is_term, p.survives_dropping(dropped), p.label_dims - dropped)

    def _group_fragment(self, p: TermFragment, g: plan.GroupSum, context: str) -> TermFragment:
        """Relabel dim ``over`` to ``into`` through a declared coordinate.

        No aggregate here either: the dim table holds one row per label and its
        coordinate was checked for containment at build time, so the join
        neither duplicates nor drops a term.

        The *key* is a separate question. Grouping merges labels of ``over``
        into one ``into``, and whether that merges two rows carrying the same
        ``var_label`` depends on where ``over`` came from. If the variable
        carries it, the merged rows have distinct labels and the key survives.
        If it arrived by broadcast — ``group_sum(x * w, over=generator)`` with
        ``x`` indexed by snapshot alone — they do not, and the terminal
        aggregate has to run.
        """

        if g.over not in p.dims:
            raise LanguageError(f"in {context}: GroupSum over '{g.over}' but the expression has dims {list(p.dims)}")
        keep = tuple(x for x in p.dims if x != g.over)
        mapping = self.dimensions[g.over].select(pl.col('val').alias(g.over), pl.col(g.coordinate).alias(g.into))
        frame = p.frame.join(mapping, on=g.over, how='inner').select(*keep, g.into, *p.carried)
        keyed = p.keyed and g.over in p.label_dims
        # a group is a sum, so §13 applies here as well: absence does not escape
        # it, which is why this constructs rather than `replace`s — see _sum_fragment
        return TermFragment((*keep, g.into), frame, p.is_term, keyed, _relabel(p.label_dims, g.over, g.into))

    def _translate_fragment(self, p: TermFragment, s: plan.Translate, context: str) -> TermFragment:
        """A pointwise remap of the dim through its ord: a row at *o*
        contributes at ``(o + by) % card``.

        Both joins are on a dim-table key, so the row count is unchanged and an
        out-of-range ordinal does not join — the zero acyclic promises. No
        window function; this is bounded-halo locality.
        """

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

        def remap(source: pl.LazyFrame, carried: list[str]) -> pl.LazyFrame:
            return (
                source.join(incoming, on=s.dimension, how='inner')
                .drop(s.dimension)
                .with_columns(moved.alias(_ORD_OUT))
                .join(outgoing, on=_ORD_OUT, how='inner')
                .select(*others, s.dimension, *carried)
            )

        frame = remap(p.frame, p.carried)
        if not s.wrap and s.fill:
            # A const fragment reads a missing row as zero, so `fill=0` needs no
            # rows at all — but `fill=1`, the identity a *product* wants, is only
            # there if it is written. Lowering guarantees a nonzero fill reaches
            # here only over a variable-free operand, so this is always the
            # const branch and never has a `var_label` to invent.
            frame = pl.concat([frame, self._filled_edge(s, card, others, s.fill)], how='vertical_relaxed')
        presence, presence_dims = None, None
        if p.presence is not None:
            # Presence is a coordinate set, so it travels through the same map,
            # and the inner join already drops whatever the edge vacated.
            presence = remap(p.presence, [])
            if not s.wrap and s.fill is not None:
                presence = pl.concat([presence, self._vacated(p, s, card, others)], how='vertical_relaxed').unique()
        elif not s.wrap and s.fill is None:
            # Nothing was absent before, and the edge now is. `None` here means
            # "present everywhere", so without this the vacated slot would
            # merely fail to join and the row would survive with the term
            # quietly gone — a different constraint, which is the shape #239
            # removed from masks and #289 removes from shift.
            presence, presence_dims = self._edge(s, card, vacated=False), (s.dimension,)
        return replace(p, frame=frame, presence=presence, presence_dims=presence_dims)

    def _filled_edge(self, s: plan.Translate, card: int, others: list[str], fill: float) -> pl.LazyFrame:
        """``(dims…, cval=fill)`` at every coordinate the shift vacated.

        Dense over *others* rather than over the rows the operand happened to
        carry: the eager lane shifts an array already reindexed to the master
        coordinates, so its fill lands at every combination, and a fill that
        appeared only where the parameter was non-sparse would be a second
        answer to the same question.
        """
        edge = self._edge(s, card, vacated=True)
        for d in others:
            edge = edge.join(self.dimensions[d].select(pl.col('val').alias(d)), how='cross')
        return edge.with_columns(pl.lit(fill, dtype=pl.Float64).alias('cval')).select(*others, s.dimension, 'cval')

    def _edge(self, s: plan.Translate, card: int, *, vacated: bool) -> pl.LazyFrame:
        """The labels of ``s.dimension`` an acyclic shift vacates, or keeps.

        The two are exact complements, so they are one filter negated rather
        than two conditions to keep in step — a fill and the presence set it
        implies must not be able to disagree about which coordinates the edge is.

        One column wide either way: an edge along this dimension is vacated for
        *every* combination of the others, so naming the others would only
        repeat the same statement.
        """
        source = pl.col('ord') - s.by
        outside = (source < 0) | (source >= card)
        return (
            self.dimensions[s.dimension]
            .filter(outside if vacated else ~outside)
            .select(pl.col('val').alias(s.dimension))
        )

    def _vacated(self, p: TermFragment, s: plan.Translate, card: int, others: list[str]) -> pl.LazyFrame:
        """The edge positions ``shift`` leaves with nothing to move in.

        Reached only under ``fill=0``, and that is the whole of what ``fill``
        does here: putting the edge coordinates back into the presence set
        makes them present-with-no-term, which is a zero contribution and a
        surviving row. Without it they stay out of the set, so absence
        propagates and the row drops — linopy v1's reading of ``.shift()``,
        which the eager lane now gets from linopy itself rather than from a
        ``fillna`` we apply on top of it.

        Only the ``shift`` edge qualifies. A coordinate the variable's own mask
        removed is genuinely absent, and remapping already dropped it above.
        """
        edge = self._edge(s, card, vacated=True)
        if not others:
            return edge
        # One vacated row per other-dim combination the variable actually has:
        # a coordinate it never covers gains nothing from an edge it never sees.
        return p.presence.select(*others).unique().join(edge, how='cross') if p.presence is not None else edge

    # ------------------------------------------------------------------
    # assembly helpers used by the executor
    # ------------------------------------------------------------------

    @staticmethod
    def constant_scalar(p: TermFragment) -> pl.LazyFrame:
        """The const fragment summed per coordinate: ``(dims…, cval)``.

        One hash group-by, rather than a lookup repeated per frame row.
        """

        if not p.dims:
            return p.frame.select(pl.col('cval').sum())
        return p.frame.group_by(p.dims).agg(pl.col('cval').sum())


def _ordinal(dim: str) -> str:
    """The frame column carrying *dim*'s position in its declared order."""
    return f'__ord {dim}__'


def _relabel(label_dims: frozenset[str], over: str, into: str) -> frozenset[str]:
    """*label_dims* after ``group_sum`` swaps *over* for *into*: the projected
    coordinate is label-determined exactly when the dim it replaces was."""
    if over not in label_dims:
        return label_dims
    return label_dims - {over} | {into}


def _falsy_if_null(condition: pl.Expr) -> pl.Expr:
    """*condition* with null read as false: a missing parameter row must
    exclude the coordinate rather than propagate. Masks are row absence."""
    return condition.fill_null(value=False)


def _compare(column: pl.Expr, op: plan.ComparisonOperator, value: float | str) -> pl.Expr:
    """One where-comparison. A string and a float are both literals here."""

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


def _propagate_absence(compiled: CompiledExpression) -> CompiledExpression:
    """Restrict every fragment to where the *whole* expression exists.

    Addition is fragment concatenation here, so ``x + size`` is two independent
    streams and each one's absence says nothing about the other. That is right
    at row level — the executor intersects the presences when it assembles the
    row — but a **reduction** consumes the expression before any row exists, and
    without this each stream would be summed over its own coordinates.

    That is the difference between ``sum(x + size, over=f)`` and
    ``sum(x, over=f) + sum(size, over=f)``, and under absence they are not the
    same question: the first sums where the summand exists, the second sums each
    operand over its own domain. Distributing one into the other would mean
    reading the absent ``size`` as a zero, which is the reading v1 exists to
    remove (SPEC §6, §7).

    Applied only where the key columns are dims the fragment carries. A
    restriction naming a dim a fragment does not have cannot speak about it, and
    a constant part lacking the summed dims is refused by ``_sum_fragment``
    before it gets here.
    """
    restrictions = [
        (p.presence_dims or p.dims, p.presence) for p in (*compiled.terms, *compiled.consts) if p.presence is not None
    ]
    if not restrictions:
        return compiled

    def restrict(p: TermFragment) -> TermFragment:
        frame = p.frame
        for on, presence in restrictions:
            if all(d in p.dims for d in on):
                frame = frame.join(presence.select(list(on)).unique(), on=list(on), how='semi')
        return replace(p, frame=frame)

    return _map_fragments(compiled, restrict)


def _map_fragments(
    compiled: CompiledExpression,
    rewrite: Callable[[TermFragment], TermFragment],
) -> CompiledExpression:
    """Apply *rewrite* to every fragment, keeping the term/const split.

    Rewriting one fragment at a time is what pointwise and bounded-halo
    locality mean; a node needing them together is global, and rejected at
    lowering.
    """
    return CompiledExpression(
        tuple(rewrite(p) for p in compiled.terms),
        tuple(rewrite(p) for p in compiled.consts),
    )


def _negate(p: TermFragment) -> TermFragment:

    return replace(p, frame=p.frame.with_columns(-pl.col(p.value_column)))


def _join_mul(a: TermFragment, c: TermFragment, is_term: bool, divide: bool = False) -> TermFragment:
    """``a * c`` (or ``a / c``) where *c* is a const fragment.

    Joins on shared dims, broadcasts the rest. The right-hand value is renamed
    first: both sides may carry ``cval``, and a suffix collision would multiply
    a column by itself. The dims *c* contributes are broadcast, so the label
    says nothing about them.
    """
    shared = [d for d in a.dims if d in c.dims]
    out_dims = a.dims + tuple(d for d in c.dims if d not in a.dims)
    right = c.frame.rename({'cval': _RHS})
    # Left for a divide, so a coordinate the divisor has no value for yields a
    # *null* coefficient instead of silently dropping the term. The row it
    # belongs to may still be masked out downstream, in which case the null goes
    # with it and nothing is reported — which is the point: the question is not
    # "is this divisor dense" but "is it defined where the model divides by it".
    how = 'left' if divide else 'inner'
    joined = a.frame.join(right, on=shared, how=how) if shared else a.frame.join(right, how='cross')

    value, rhs = pl.col(a.value_column), pl.col(_RHS)
    combined = value / rhs if divide else value * rhs
    out = 'coeff' if is_term else 'cval'
    carried = ['var_label', out] if is_term else [out]
    frame = joined.with_columns(combined.alias(out)).select(*out_dims, *carried)
    # *c* is variable-free, so it contributes no absence: a sparse coefficient
    # zeroes a term, it does not unmake the variable underneath it. `out_dims`
    # may be wider than `a.dims`, which is why the presence key travels with the
    # frame rather than being re-derived from dims here (#345).
    return replace(a, dims=out_dims, frame=frame, is_term=is_term, keyed=a.keyed and c.keyed)
