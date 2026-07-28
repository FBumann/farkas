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

What it does not do: line-breaking (a wide equation runs off the page), and it
never renders a ``where`` as anything but a set-builder condition, because that
is what a mask means here.

Usage::

    import farkas as fk

    print(fk.to_latex('model.yaml'))
    print(fk.to_latex('model.yaml', standalone=True))  # compilable document

or from a shell::

    python -m farkas latex model.yaml --standalone -o model.tex
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, assert_never

from farkas.api import load_schema
from farkas.dimensions import dims_of
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
    from pathlib import Path
    from typing import Any

    from farkas.schema import MathSchema

__all__ = ['to_latex']

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


def _name_symbol(name: str) -> str:
    """``p`` → ``p``; ``load`` → ``\\mathit{load}``; ``p_max`` → ``p^{\\mathrm{max}}``.

    The tail after the first underscore becomes a superscript rather than a
    subscript, because subscripts are spoken for: they carry the dimensions,
    and a symbol that puts a qualifier there collides with its own indices.
    """
    head, _, tail = name.partition('_')
    base = head if len(head) == 1 else rf'\mathit{{{head}}}' if head else r'\square'
    if not tail:
        return base
    return rf'{base}^{{\mathrm{{{tail.replace("_", ",")}}}}}'


class Symbols:
    """Index and set letters for the declared dimensions, assigned once.

    Both are derived, not configured: a spike that asks for a symbol table
    before it will print anything is a spike nobody runs. Collisions resolve
    by walking further into the name and then through the alphabet, so the
    assignment is deterministic given the declaration order.
    """

    def __init__(self, schema: MathSchema) -> None:
        self.index: dict[str, str] = {}
        self.set: dict[str, str] = {}
        taken_index: set[str] = set()
        taken_set: set[str] = set()
        for dim in schema.dimensions:
            letter = _first_free(_index_candidates(dim), taken_index)
            taken_index.add(letter)
            self.index[dim] = letter if len(letter) == 1 else rf'\mathrm{{{letter}}}'
            upper = _first_free(_set_candidates(dim, letter), taken_set)
            taken_set.add(upper)
            self.set[dim] = rf'\mathcal{{{upper}}}'


def _index_candidates(dim: str) -> list[str]:
    alias = _INDEX_ALIASES.get(dim)
    letters = [c for c in dim.lower() if c.isalpha()]
    return [*([alias] if alias else []), *letters, *string.ascii_lowercase, dim]


def _set_candidates(dim: str, index_letter: str) -> list[str]:
    letters = [c.upper() for c in dim if c.isalpha()]
    return [index_letter[0].upper(), *letters, *string.ascii_uppercase]


def _first_free(candidates: list[str], taken: set[str]) -> str:
    return next((c for c in candidates if c not in taken), candidates[-1])


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
            return ctx.indexed(_name_symbol(node.name), list(self.schema.parameters[node.name].dims)), _ATOM

        if isinstance(node, VariableNode):
            return ctx.indexed(_name_symbol(node.name), list(self.schema.variables[node.name].foreach)), _ATOM

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
            return rf'{ctx.indexed(_name_symbol(node.name), dims)} \text{{ is defined}}', 2

        if isinstance(node, VariableDefinedNode):
            dims = list(self.schema.variables[node.name].foreach)
            return rf'{ctx.indexed(_name_symbol(node.name), dims)} \text{{ exists}}', 2

        if isinstance(node, ParameterComparisonNode):
            dims = list(self.schema.parameters[node.name].dims)
            left = ctx.indexed(_name_symbol(node.name), dims)
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
        return ctx.indexed(_name_symbol(value), list(renderer.schema.parameters[value].dims))
    return _number(value)


def _variable_rows(renderer: _Renderer, schema: MathSchema, ns: Namespace) -> list[str]:
    rows = []
    for name, block in schema.variables.items():
        ctx = renderer.context()
        symbol = ctx.indexed(_name_symbol(name), list(block.foreach))
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
            rf'--- \texttt{{{_text(d)}}}' + _coords_note(renderer, schema, d)
            for d in schema.dimensions
        ]
        lines.append(_description('Sets', entries))
    if schema.parameters:
        entries = [
            rf'\item[${_name_symbol(p)}$] \texttt{{{_text(p)}}}{_dims_note(renderer, list(block.dims))}'
            for p, block in schema.parameters.items()
        ]
        lines.append(_description('Parameters', entries))
    if schema.variables:
        entries = [
            rf'\item[${_name_symbol(v)}$] \texttt{{{_text(v)}}}{_dims_note(renderer, list(block.foreach))}'
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
    standalone: bool = False,
    legend: bool = True,
    numbered: bool = True,
) -> str:
    """Render *model* as LaTeX (amsmath ``align`` environments).

    Accepts anything :func:`farkas.load_schema` accepts. ``standalone`` wraps
    the result in a compilable document; ``legend`` prepends the sets /
    parameters / variables table; ``numbered`` chooses ``align`` over
    ``align*``.

    The model is validated on the way in, so a file that does not compile does
    not print either — the error is the same one :func:`farkas.check` raises.
    """
    schema = expand_piecewise(load_schema(model))
    ns = Namespace.of(schema)
    symbols = Symbols(schema)
    renderer = _Renderer(schema, symbols)

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
