"""Render a validated model as LaTeX — a *reading* of the math, not a lane.

SPIKE. This is a third consumer of the core AST, and it is deliberately not a
backend: it produces no model, binds no data and never touches the plan. It
exists because the file's whole point is that the math is declared, and a
declared thing can be printed the way a paper prints it — which is also the
cheapest review tool we have for "does this YAML say what I meant".

It reads the same seam both lanes read (hard rule 1): expand ``piecewise:``,
resolve names to typed nodes, then walk. Because expansion runs first, a
``piecewise:`` block prints as the λ-formulation it *is* rather than as the
sugar it was written as — which is the honest rendering, if a verbose one.

Symbols are **derived** by default, so it prints with no setup at all — and
derivation aims at unambiguous rather than beautiful. A :class:`SymbolTable`
(a sidecar YAML, ``--symbols``) is what makes it conventional: it is
presentation, so it stays out of ``MathSchema``, which is the versioned
contract every lane sees.

What it does not do: line-breaking (a wide equation runs off the page), and it
never renders a ``where`` as anything but a set-builder condition, because that
is what a mask means here.

Usage::

    import farkas as fk

    print(fk.to_latex('model.yaml'))
    print(fk.to_latex('model.yaml', standalone=True))  # compilable document
    print(fk.to_latex('model.yaml', symbols='model.symbols.yaml'))

or from a shell::

    python -m farkas latex model.yaml --symbols model.symbols.yaml --standalone -o model.tex
"""

from __future__ import annotations

import difflib
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, assert_never

from farkas._yaml import read_yaml
from farkas.api import load_schema
from farkas.dimensions import dims_of
from farkas.errors import SchemaError
from farkas.expression_parser import (
    ArithmeticNode,
    BinaryOperatorNode,
    ComparisonNode,
    CoordinateNode,
    DimensionNode,
    FunctionCallNode,
    NameNode,
    NumberNode,
    ParameterNode,
    UnaryOperatorNode,
    VariableNode,
)
from farkas.piecewise import expand_piecewise
from farkas.resolution import Namespace, expression_of, where_of
from farkas.schema import equation_name
from farkas.where_parser import (
    AndNode,
    BooleanLiteralNode,
    DimensionComparisonNode,
    NotNode,
    OrNode,
    ParameterComparisonNode,
    ParameterDefinedNode,
    UnresolvedComparisonNode,
    UnresolvedNameNode,
    VariableDefinedNode,
    WhereNode,
)

if TYPE_CHECKING:
    from farkas.schema import MathSchema

__all__ = ['SymbolTable', 'to_latex']

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

#: Dimensions whose conventional index letter is not their own first letter.
#: Small on purpose — a lookup table of everybody's naming habits is a
#: maintenance sink; anything unlisted falls back to its own initial.
_INDEX_ALIASES = {'snapshot': 't', 'snapshots': 't', 'time': 't', 'timestep': 't', 'timesteps': 't'}

_RELATION = {'==': '=', '<=': r'\le', '>=': r'\ge'}
_PREDICATE = {'==': '=', '!=': r'\neq', '<=': r'\le', '>=': r'\ge', '<': '<', '>': '>'}

#: Operator precedence, for deciding parentheses. A reduction sits at the
#: bottom with ``+``: an unparenthesised ``\sum`` reads as capturing whatever
#: follows it, so as a factor it has to be bracketed.
_PRECEDENCE = {'+': 1, '-': 1, '*': 2, '/': 2, '**': 4}
_ATOM = 5

_TEXT_ESCAPES = {
    '\\': r'\textbackslash{}',
    '&': r'\&',
    '%': r'\%',
    '$': r'\$',
    '#': r'\#',
    '_': r'\_',
    '{': r'\{',
    '}': r'\}',
    '~': r'\textasciitilde{}',
    '^': r'\textasciicircum{}',
}


def _text(s: str) -> str:
    """A model name as LaTeX text — ``power_balance`` must not eat its underscore."""
    return ''.join(_TEXT_ESCAPES.get(c, c) for c in s)


def _number(value: float) -> str:
    if value == float('inf'):
        return r'\infty'
    if value == float('-inf'):
        return r'-\infty'
    if value == int(value):
        return str(int(value))
    return repr(value)


def _word(name: str) -> str:
    """One name as one symbol: a letter stays a letter, a word is upright-italic."""
    if not name:
        return r'\square'
    escaped = name.replace('_', r'\_')
    return name if len(name) == 1 else rf'\mathit{{{escaped}}}'


def _derive_name_symbol(name: str, declared: frozenset[str]) -> str:
    """``p`` → ``p``; ``load`` → ``\\mathit{load}``; ``p_max`` → ``p^{\\mathrm{max}}``.

    An underscore is a **qualifier** only when what precedes it is a symbol in
    its own right — a single letter (``p_max``), or another declared name
    (``soc_max``, ``bp_x``). Everywhere else it is word separation, and
    splitting there produced nonsense: ``marginal_cost`` is not *marginal*
    raised to *cost*, and ``shut_down`` is one word with a down-arrow's worth
    of meaning in it.

    So the fallback prints the name as written, underscore and all. That is
    plain rather than beautiful, and deliberately: a derived symbol has to be
    *unambiguous*, and a symbol table (``--symbols``) is what makes it pretty.
    A qualifier lands in the superscript because the subscript slot is spoken
    for — it carries the dimensions.
    """
    head, _, tail = name.partition('_')
    if tail and (len(head) == 1 or head in declared):
        return rf'{_word(head)}^{{\mathrm{{{tail.replace("_", ",")}}}}}'
    return _word(name)


class Symbols:
    """How every declared name prints: overrides first, derivation for the rest.

    Assignment order is load-bearing. Name symbols are settled *before*
    dimension indices, so an index can be kept off a letter a variable already
    owns — without that, a model with a dimension ``plant`` and a variable
    ``p`` renders ``p_{t,p}`` and no reader can tell which ``p`` is which.
    Deriving the two independently is exactly how that got through.
    """

    def __init__(self, schema: MathSchema, table: SymbolTable | None = None) -> None:
        table = table or SymbolTable()
        declared = frozenset({*schema.dimensions, *schema.parameters, *schema.variables})

        self.name: dict[str, str] = {
            name: table.names.get(name) or _derive_name_symbol(name, declared)
            for name in (*schema.parameters, *schema.variables)
        }
        # Only single-letter symbols can be mistaken for an index; a
        # `\mathit{load}` never collides with a `t`.
        spoken_for = {s for s in self.name.values() if len(s) == 1}

        self.index: dict[str, str] = {}
        self.set: dict[str, str] = {}
        taken_index, taken_set = set(spoken_for), set()
        for dim in schema.dimensions:
            override = table.indices.get(dim)
            letter = override or _first_free(_index_candidates(dim), taken_index)
            taken_index.add(letter)
            self.index[dim] = letter if len(letter) <= 1 or override else rf'\mathrm{{{letter}}}'
            given = table.sets.get(dim)
            upper = _first_free(_set_candidates(dim, letter), taken_set)
            taken_set.add(upper)
            self.set[dim] = given or rf'\mathcal{{{upper}}}'

        self.description: dict[str, str] = dict(table.descriptions)


def _index_candidates(dim: str) -> list[str]:
    alias = _INDEX_ALIASES.get(dim)
    letters = [c for c in dim.lower() if c.isalpha()]
    return [*([alias] if alias else []), *letters, *string.ascii_lowercase, dim]


def _set_candidates(dim: str, index_letter: str) -> list[str]:
    first = next((c for c in index_letter if c.isalpha()), '')
    letters = [c.upper() for c in dim if c.isalpha()]
    return [*([first.upper()] if first else []), *letters, *string.ascii_uppercase]


def _first_free(candidates: list[str], taken: set[str]) -> str:
    return next((c for c in candidates if c not in taken), candidates[-1])


# ---------------------------------------------------------------------------
# the symbol table (a sidecar file, not the model)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolTable:
    """How a *reader* wants the model to print — kept out of the model.

    This is presentation, and presentation is not language: nothing here
    changes what the file means, no lane reads it, and a model with no table
    still renders. So it lives in its own file rather than as keys on
    ``MathSchema``, which is the versioned contract every consumer sees.

    Its own format, deliberately strict: a name it does not recognise is an
    error naming the near miss, because the failure mode of a silent typo is a
    symbol that simply never applies and a reader who never finds out::

        dimensions:
          snapshot: {index: t, set: "\\\\mathcal{T}"}
          plant:    {index: n}
        names:
          marginal_cost: "c^{\\\\mathrm{marg}}"
        descriptions:
          snapshot: hourly, over one year
    """

    indices: dict[str, str] = field(default_factory=dict)
    sets: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, source: str | Path | Mapping[str, Any]) -> SymbolTable:
        raw = dict(source) if isinstance(source, Mapping) else read_yaml(Path(source))
        unknown = set(raw) - {'dimensions', 'names', 'descriptions'}
        if unknown:
            msg = (
                f'symbol table: unknown section(s) {sorted(unknown)}. Valid sections: dimensions, names, descriptions.'
            )
            raise SchemaError(msg)

        indices: dict[str, str] = {}
        sets: dict[str, str] = {}
        for dim, spec in (raw.get('dimensions') or {}).items():
            if not isinstance(spec, Mapping):
                msg = f"symbol table: dimension '{dim}' must be a mapping like {{index: t, set: '\\\\mathcal{{T}}'}}"
                raise SchemaError(msg)
            extra = set(spec) - {'index', 'set'}
            if extra:
                msg = f"symbol table: dimension '{dim}' has unknown key(s) {sorted(extra)}. Valid keys: index, set."
                raise SchemaError(msg)
            if 'index' in spec:
                indices[dim] = str(spec['index'])
            if 'set' in spec:
                sets[dim] = str(spec['set'])

        return cls(
            indices=indices,
            sets=sets,
            names={k: str(v) for k, v in (raw.get('names') or {}).items()},
            descriptions={k: str(v) for k, v in (raw.get('descriptions') or {}).items()},
        )

    def checked_against(self, schema: MathSchema) -> SymbolTable:
        """Reject entries naming nothing in *schema*, with the near miss."""
        dims = set(schema.dimensions)
        everything = dims | set(schema.parameters) | set(schema.variables)
        errors = [
            *(_unknown_entry(d, 'dimensions', dims) for d in {*self.indices, *self.sets} - dims),
            *(_unknown_entry(n, 'names', everything - dims) for n in set(self.names) - everything),
            *(_unknown_entry(n, 'descriptions', everything) for n in set(self.descriptions) - everything),
        ]
        if errors:
            raise SchemaError('\n'.join(sorted(errors)))
        return self


def _unknown_entry(name: str, section: str, known: set[str]) -> str:
    near = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.6)
    fix = f"Did you mean '{near[0]}'?" if near else f'Declared: {", ".join(sorted(known)) or "nothing"}.'
    return f"symbol table: '{name}' under {section}: is not declared by the model. {fix}"


# ---------------------------------------------------------------------------
# the walking context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Context:
    """What a subscript means at this point in the tree.

    ``offsets`` is how ``roll``/``shift`` are rendered: neither emits an
    operator of its own, they re-index their operand, so the translation shows
    up at the *leaves* underneath — which is exactly what the plan's
    ``Translate`` node says it does.
    """

    symbols: Symbols
    offsets: dict[str, tuple[int, bool]] = field(default_factory=dict)

    def translated(self, dim: str, by: int, *, wrap: bool) -> _Context:
        previous, previous_wrap = self.offsets.get(dim, (0, wrap))
        return _Context(self.symbols, {**self.offsets, dim: (previous + by, wrap or previous_wrap)})

    def subscript(self, dim: str) -> str:
        base = self.symbols.index[dim]
        by, wrap = self.offsets.get(dim, (0, False))
        if by == 0:
            return base
        operator = (r'\ominus' if wrap else '-') if by > 0 else (r'\oplus' if wrap else '+')
        return f'{base} {operator} {abs(by)}'

    def indexed(self, symbol: str, dims: list[str]) -> str:
        if not dims:
            return symbol
        return f'{symbol}_{{{",".join(self.subscript(d) for d in dims)}}}'


# ---------------------------------------------------------------------------
# expressions
# ---------------------------------------------------------------------------


class _Renderer:
    """Walks resolved expressions. Stateful only in what it has *noticed* —
    whether any ``roll`` appeared, which the legend needs to explain ⊖."""

    def __init__(self, schema: MathSchema, symbols: Symbols) -> None:
        self.schema = schema
        self.symbols = symbols
        self.saw_wraparound = False

    def context(self) -> _Context:
        return _Context(self.symbols)

    # -- arithmetic --------------------------------------------------------

    def arithmetic(self, node: ArithmeticNode, ctx: _Context, *, need: int = 0) -> str:
        text, precedence = self._arithmetic(node, ctx)
        return rf'\left( {text} \right)' if precedence < need else text

    def _arithmetic(self, node: ArithmeticNode, ctx: _Context) -> tuple[str, int]:
        if isinstance(node, NumberNode):
            return _number(node.value), _ATOM if node.value >= 0 else 1

        if isinstance(node, ParameterNode):
            return ctx.indexed(ctx.symbols.name[node.name], list(self.schema.parameters[node.name].dims)), _ATOM

        if isinstance(node, VariableNode):
            return ctx.indexed(ctx.symbols.name[node.name], list(self.schema.variables[node.name].foreach)), _ATOM

        if isinstance(node, UnaryOperatorNode):
            operand = self.arithmetic(node.operand, ctx, need=2)
            return (f'-{operand}' if node.op == '-' else operand), 1

        if isinstance(node, BinaryOperatorNode):
            return self._binary(node, ctx)

        if isinstance(node, FunctionCallNode):
            return self._call(node, ctx)

        if isinstance(node, (NameNode, DimensionNode, CoordinateNode)):
            # A NameNode here means resolution was skipped; a bare dimension or
            # coordinate in a value position is a language error caught long
            # before this module runs.
            msg = f'{type(node).__name__} reached the LaTeX renderer; resolve the expression first.'
            raise AssertionError(msg)

        assert_never(node)

    def _binary(self, node: BinaryOperatorNode, ctx: _Context) -> tuple[str, int]:
        if node.op == '/':
            top = self.arithmetic(node.left, ctx)
            bottom = self.arithmetic(node.right, ctx)
            return rf'\frac{{{top}}}{{{bottom}}}', _ATOM
        if node.op == '**':
            base = self.arithmetic(node.left, ctx, need=_ATOM)
            exponent = self.arithmetic(node.right, ctx)
            return f'{base}^{{{exponent}}}', _PRECEDENCE['**']
        precedence = _PRECEDENCE[node.op]
        left = self.arithmetic(node.left, ctx, need=precedence)
        # `a - (b - c)` and `a - (b + c)` need the bracket; `a - b*c` does not.
        right = self.arithmetic(node.right, ctx, need=precedence + (1 if node.op == '-' else 0))
        operator = r'\cdot' if node.op == '*' else node.op
        return f'{left} {operator} {right}', precedence

    def _call(self, node: FunctionCallNode, ctx: _Context) -> tuple[str, int]:
        if node.name in ('roll', 'shift'):
            dim, amount = next(iter(node.kwargs.items()))
            assert isinstance(amount, NumberNode)
            wrap = node.name == 'roll'
            self.saw_wraparound = self.saw_wraparound or wrap
            return self._arithmetic(node.args[0], ctx.translated(dim, int(amount.value), wrap=wrap))

        over = node.kwargs['over']
        assert isinstance(over, DimensionNode)
        domain = f'{self.symbols.index[over.name]} \\in {self.symbols.set[over.name]}'
        if node.name == 'group_sum':
            by = node.kwargs['by']
            assert isinstance(by, CoordinateNode)
            mapping = rf'\mathrm{{{_text(by.name)}}}({self.symbols.index[over.name]})'
            domain = rf'{domain} \,:\, {mapping} = {ctx.subscript(by.into)}'
        return rf'\sum_{{{domain}}} {self.reduction_body(node.args[0], ctx)}', _PRECEDENCE['+']

    def reduction_body(self, node: ArithmeticNode, ctx: _Context) -> str:
        r"""What sits to the right of a ``\sum``, bracketed only where it must be.

        A ``\sum`` binds everything up to the next ``+`` or ``-`` at its own
        level, so an additive body needs the bracket and nothing else does —
        including a nested reduction, since ``\sum_t \sum_g x`` is unambiguous.
        Going through the precedence rule instead would bracket that too, and
        a renderer that brackets everything is one nobody trusts to bracket
        the thing that matters.
        """
        additive = isinstance(node, UnaryOperatorNode) or (
            isinstance(node, BinaryOperatorNode) and node.op in ('+', '-')
        )
        return self.arithmetic(node, ctx, need=2 if additive else 0)

    def comparison(self, node: ComparisonNode, ctx: _Context) -> tuple[str, str]:
        """(left, ``relation`` + right) — split so ``align`` can align on it."""
        return self.arithmetic(node.left, ctx), f'{_RELATION[node.op]} {self.arithmetic(node.right, ctx)}'

    # -- where strings -----------------------------------------------------

    def where(self, node: WhereNode, ctx: _Context, *, need: int = 0) -> str:
        text, precedence = self._where(node, ctx)
        return rf'\left( {text} \right)' if precedence < need else text

    def _where(self, node: WhereNode, ctx: _Context) -> tuple[str, int]:
        if isinstance(node, BooleanLiteralNode):
            return (r'\top' if node.value else r'\bot'), _ATOM

        if isinstance(node, ParameterDefinedNode):
            dims = list(self.schema.parameters[node.name].dims)
            return rf'{ctx.indexed(ctx.symbols.name[node.name], dims)} \text{{ is defined}}', 2

        if isinstance(node, VariableDefinedNode):
            dims = list(self.schema.variables[node.name].foreach)
            return rf'{ctx.indexed(ctx.symbols.name[node.name], dims)} \text{{ exists}}', 2

        if isinstance(node, ParameterComparisonNode):
            dims = list(self.schema.parameters[node.name].dims)
            left = ctx.indexed(ctx.symbols.name[node.name], dims)
            return f'{left} {_PREDICATE[node.op]} {_literal(node.value)}', 2

        if isinstance(node, DimensionComparisonNode):
            return f'{ctx.subscript(node.name)} {_PREDICATE[node.op]} {_literal(node.value)}', 2

        if isinstance(node, NotNode):
            return rf'\neg {self.where(node.operand, ctx, need=2)}', 2

        if isinstance(node, AndNode):
            return rf'{self.where(node.left, ctx, need=1)} \wedge {self.where(node.right, ctx, need=1)}', 1

        if isinstance(node, OrNode):
            return rf'{self.where(node.left, ctx, need=1)} \vee {self.where(node.right, ctx, need=1)}', 0

        if isinstance(node, (UnresolvedNameNode, UnresolvedComparisonNode)):
            msg = f'{type(node).__name__} reached the LaTeX renderer; resolve the where string first.'
            raise AssertionError(msg)

        assert_never(node)


def _literal(value: float | str) -> str:
    return _number(value) if isinstance(value, (int, float)) else rf'\text{{{_text(str(value))}}}'


# ---------------------------------------------------------------------------
# declarations
# ---------------------------------------------------------------------------


def _quantifier(symbols: Symbols, dims: list[str], condition: str | None) -> str:
    if not dims and condition is None:
        return ''
    over = r',\ '.join(f'{symbols.index[d]} \\in {symbols.set[d]}' for d in dims)
    if condition is None:
        return rf'\forall\, {over}'
    return rf'\forall\, {over} \,:\, {condition}' if over else rf'\text{{where }} {condition}'


def _conjoin(renderer: _Renderer, ctx: _Context, *nodes: WhereNode | None) -> str | None:
    parts = [renderer.where(n, ctx, need=1) for n in nodes if n is not None]
    if not parts:
        return None
    return r' \wedge '.join(parts)


def _align(rows: list[str], *, numbered: bool = True) -> str:
    environment = 'align' if numbered else 'align*'
    body = ' \\\\\n'.join(rows)
    return f'\\begin{{{environment}}}\n{body}\n\\end{{{environment}}}'


def _sorted_dims(schema: MathSchema, dims: frozenset[str]) -> list[str]:
    order = list(schema.dimensions)
    return sorted(dims, key=order.index)


def _bound(renderer: _Renderer, ctx: _Context, value: float | str) -> str:
    if isinstance(value, str):
        return ctx.indexed(ctx.symbols.name[value], list(renderer.schema.parameters[value].dims))
    return _number(value)


def _variable_rows(renderer: _Renderer, schema: MathSchema, ns: Namespace) -> list[str]:
    rows = []
    for name, block in schema.variables.items():
        ctx = renderer.context()
        symbol = ctx.indexed(ctx.symbols.name[name], list(block.foreach))
        where = where_of(block.where, ns, f"variable '{name}'", self_variable=name)
        quantifier = _quantifier(renderer.symbols, list(block.foreach), _conjoin(renderer, ctx, where))
        lower, upper = block.bounds.lower, block.bounds.upper

        if block.binary:
            body = rf'{symbol} & \in \{{0, 1\}}'
        else:
            domain = r'\mathbb{Z}' if block.integer else r'\mathbb{R}'
            unbounded_below = lower == float('-inf')
            unbounded_above = upper == float('inf')
            if unbounded_below and unbounded_above:
                body = rf'{symbol} & \in {domain}'
            elif unbounded_below:
                body = rf'{symbol} & \le {_bound(renderer, ctx, upper)}'
            elif unbounded_above:
                body = rf'{symbol} & \ge {_bound(renderer, ctx, lower)}'
            else:
                body = rf'{_bound(renderer, ctx, lower)} \le {symbol} & \le {_bound(renderer, ctx, upper)}'
            if block.integer and not (unbounded_below and unbounded_above):
                body = rf'{body},\ {symbol} \in \mathbb{{Z}}'
        rows.append(rf'\text{{{_text(name)}}} && {body} && {quantifier}')
    return rows


def _constraint_rows(renderer: _Renderer, schema: MathSchema, ns: Namespace) -> list[str]:
    rows = []
    for name, block in schema.constraints.items():
        for i, equation in enumerate(block.equations):
            label = equation_name(name, i, len(block.equations))
            context = f"constraint '{label}'"
            node = expression_of(equation.expression, schema, ns, context)
            if not isinstance(node, ComparisonNode):
                msg = f'{context}: expected a comparison, got {type(node).__name__}'
                raise AssertionError(msg)
            ctx = renderer.context()
            left, right = renderer.comparison(node, ctx)
            condition = _conjoin(
                renderer,
                ctx,
                where_of(block.where, ns, context),
                where_of(equation.where, ns, context),
            )
            quantifier = _quantifier(renderer.symbols, list(block.foreach), condition)
            rows.append(rf'\text{{{_text(label)}}} && {left} & {right} && {quantifier}')
    return rows


def _objective_rows(renderer: _Renderer, schema: MathSchema, ns: Namespace) -> list[str]:
    rows = []
    for name, block in schema.objectives.items():
        operator = r'\min' if block.sense == 'minimize' else r'\max'
        terms = []
        for i, equation in enumerate(block.equations):
            context = f"objective '{name}'"
            node = expression_of(equation.expression, schema, ns, context)
            assert not isinstance(node, ComparisonNode)
            ctx = renderer.context()
            # An objective sums each term over every dim that term carries —
            # the reduction is implied by the declaration, so it is spelled out
            # here rather than left for the reader to assume.
            dims = _sorted_dims(schema, dims_of(node, schema, context))
            condition = _conjoin(renderer, ctx, where_of(equation.where, ns, context))
            body = renderer.reduction_body(node, ctx) if dims else renderer.arithmetic(node, ctx)
            if dims:
                domain = r',\ '.join(f'{renderer.symbols.index[d]} \\in {renderer.symbols.set[d]}' for d in dims)
                if condition is not None:
                    domain = rf'{domain} \,:\, {condition}'
                body = rf'\sum_{{{domain}}} {body}'
            elif condition is not None:
                body = rf'{body} \quad \text{{where }} {condition}'
            terms.append(body if i == 0 else rf'{{}}+ {body}')
        rows.append(rf'\text{{{_text(name)}}} && {operator} \quad & {" ".join(terms)} &&')
    return rows


# ---------------------------------------------------------------------------
# legend
# ---------------------------------------------------------------------------


def _legend(renderer: _Renderer, schema: MathSchema) -> str:
    lines = []
    if schema.dimensions:
        entries = [
            rf'\item[${renderer.symbols.set[d]}$] index ${renderer.symbols.index[d]}$ '
            rf'--- \texttt{{{_text(d)}}}' + _coords_note(renderer, schema, d) + _gloss(renderer, d)
            for d in schema.dimensions
        ]
        lines.append(_description('Sets', entries))
    if schema.parameters:
        entries = [
            rf'\item[${renderer.symbols.name[p]}$] \texttt{{{_text(p)}}}'
            rf'{_dims_note(renderer, list(block.dims))}{_gloss(renderer, p)}'
            for p, block in schema.parameters.items()
        ]
        lines.append(_description('Parameters', entries))
    if schema.variables:
        entries = [
            rf'\item[${renderer.symbols.name[v]}$] \texttt{{{_text(v)}}}'
            rf'{_dims_note(renderer, list(block.foreach))}{_gloss(renderer, v)}'
            for v, block in schema.variables.items()
        ]
        lines.append(_description('Variables', entries))
    if renderer.saw_wraparound:
        lines.append(
            r'\noindent $t \ominus k$ denotes cyclic translation: index $t-k$ taken '
            r'modulo the size of the dimension (\texttt{roll}). Plain $t-k$ '
            r'(\texttt{shift}) has no wraparound --- terms translated past the '
            r'edge are simply absent.'
        )
    return '\n\n'.join(lines)


def _gloss(renderer: _Renderer, name: str) -> str:
    """The legend's trailing prose for one name, when the table supplies it.

    It goes *after* the name and its dims, never instead of them: the YAML
    name is what a reader types to find the declaration, and a legend that
    replaces it with prose makes the symbol table the only way back to the
    model.
    """
    described = renderer.symbols.description.get(name)
    return f' --- {described}' if described else ''


def _description(title: str, entries: list[str]) -> str:
    body = '\n'.join(entries)
    return f'\\paragraph{{{title}}}\n\\begin{{description}}\n{body}\n\\end{{description}}'


def _dims_note(renderer: _Renderer, dims: list[str]) -> str:
    if not dims:
        return ' (scalar)'
    return ' over $' + r' \times '.join(renderer.symbols.set[d] for d in dims) + '$'


def _coords_note(renderer: _Renderer, schema: MathSchema, dim: str) -> str:
    coords = schema.dimensions[dim].coords
    if not coords:
        return ''
    maps = r',\ '.join(
        rf'\mathrm{{{_text(c)}}}\!: {renderer.symbols.set[dim]} \to {renderer.symbols.set[target]}'
        for c, target in coords.items()
    )
    return f' with ${maps}$'


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

_PREAMBLE = r"""\documentclass[11pt]{article}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage[margin=2.5cm]{geometry}
\allowdisplaybreaks
\begin{document}
"""


def to_latex(
    model: str | Path | dict[str, Any] | MathSchema,
    *,
    symbols: str | Path | Mapping[str, Any] | SymbolTable | None = None,
    standalone: bool = False,
    legend: bool = True,
    numbered: bool = True,
) -> str:
    """Render *model* as LaTeX (amsmath ``align`` environments).

    Accepts anything :func:`farkas.load_schema` accepts. ``symbols`` is an
    optional :class:`SymbolTable` — a path, a mapping, or the object — saying
    how names should print; everything it does not name is derived.
    ``standalone`` wraps the result in a compilable document; ``legend``
    prepends the sets / parameters / variables table; ``numbered`` chooses
    ``align`` over ``align*``.

    The model is validated on the way in, so a file that does not compile does
    not print either — the error is the same one :func:`farkas.check` raises.
    A symbol table is checked against it too: an entry naming nothing in the
    model is an error, since the alternative is a symbol that silently never
    applies.
    """
    schema = expand_piecewise(load_schema(model))
    ns = Namespace.of(schema)
    table = symbols if isinstance(symbols, SymbolTable) else SymbolTable.load(symbols or {})
    symbol_map = Symbols(schema, table.checked_against(schema))
    renderer = _Renderer(schema, symbol_map)

    # Rendering runs before the legend: `saw_wraparound` is something the walk
    # discovers, and the legend has to explain what the walk actually emitted.
    sections: list[tuple[str, list[str]]] = [
        ('Objective', _objective_rows(renderer, schema, ns)),
        ('Subject to', _constraint_rows(renderer, schema, ns)),
        ('Variable domains', _variable_rows(renderer, schema, ns)),
    ]

    blocks = []
    if legend:
        blocks.append(_legend(renderer, schema))
    for title, rows in sections:
        if rows:
            blocks.append(f'\\paragraph{{{title}}}\n{_align(rows, numbered=numbered)}')
    body = '\n\n'.join(blocks) + '\n'
    return f'{_PREAMBLE}\n{body}\n\\end{{document}}\n' if standalone else body
