"""Load-time validation of expression and where strings.

Parses every expression and where string in a schema before any linopy
call, so typos and malformed math fail at load time with the offending
component named — not mid-build.

Where strings are only checked for syntax: unknown names in a where
evaluate to False by design (see SPEC section 6.2), so they are not
name-checked here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from linopy_yaml.expansion import expand, parse_and_expand, parse_template
from linopy_yaml.expression_parser import (
    ArithNode,
    BinOpNode,
    CompareNode,
    FuncCallNode,
    NameNode,
    NumberNode,
    UnaryOpNode,
)
from linopy_yaml.helpers import get_helper
from linopy_yaml.where_parser import parse_where

if TYPE_CHECKING:
    from collections.abc import Iterable

    from linopy_yaml.schema import MathSchema


def validate_expressions(
    schema: MathSchema,
    *,
    known_variables: Iterable[str] = (),
) -> None:
    """Validate all expression and where strings in *schema*.

    Checks, per constraint/objective equation:

    - the expression parses;
    - constraints contain exactly one comparison, objectives none;
    - every referenced name is a declared variable or parameter;
    - every helper function is a built-in;
    - where strings (constraint-, equation-, and variable-level) parse.

    Parameters
    ----------
    schema : MathSchema
        The schema to validate.
    known_variables : Iterable[str]
        Names valid in addition to those declared in *schema* — used by
        ``compat.extend()``, where expressions may reference variables
        already present on the model. Parameters get no such widening: a
        YAML file declares every parameter it uses (hard rule 5).

    Raises
    ------
    ValueError
        Listing every problem found, one per line.
    """
    variables = set(schema.variables) | set(known_variables)
    parameters = set(schema.parameters)

    errors: list[str] = []

    for mname, macro in schema.macros.items():
        context = f"Macro '{mname}'"
        try:
            get_helper(mname)
        except NameError:
            pass
        else:
            errors.append(f'{context}: collides with a helper function of the same name.')
        try:
            body_ast = parse_template(mname, macro, context)
            body_ast = expand(body_ast, schema, context)
        except ValueError as e:
            errors.append(str(e) if str(e).startswith(context) else f'{context}: {e}')
            continue
        # macros are schema-local, so every free name in the template must be
        # a formal or a name declared in *this* schema — checkable even for
        # macros the current model never calls
        formals = {*macro.args, *macro.kwargs}
        _check_names(
            body_ast,
            macro.template,
            context,
            variables | formals,
            parameters | formals,
            errors,
        )

    for ename, body in schema.expressions.items():
        context = f"Named expression '{ename}'"
        ast = _check_parse(body, schema, context, errors)
        if ast is None:
            continue
        if isinstance(ast, CompareNode):
            errors.append(f'{context}: must not contain a comparison operator.\nGot: {body!r}')
            continue
        _check_names(ast, body, context, variables, parameters, errors)

    for vname, vdef in schema.variables.items():
        _check_where(vdef.where, f"Variable '{vname}'", errors)

    for cname, cdef in schema.constraints.items():
        _check_where(cdef.where, f"Constraint '{cname}'", errors)
        for i, eq in enumerate(cdef.equations):
            where = f"Constraint '{cname}', equation {i}"
            _check_where(eq.where, where, errors)
            ast = _check_parse(eq.expression, schema, where, errors)
            if ast is None:
                continue
            if not isinstance(ast, CompareNode):
                errors.append(
                    f'{where}: expression must contain exactly one '
                    f'comparison operator (<=, >=, ==).\n'
                    f'Got: {eq.expression!r}'
                )
                continue
            _check_names(ast.left, eq.expression, where, variables, parameters, errors)
            _check_names(ast.right, eq.expression, where, variables, parameters, errors)

    for oname, odef in schema.objectives.items():
        for i, eq in enumerate(odef.equations):
            where = f"Objective '{oname}', equation {i}"
            _check_where(eq.where, where, errors)
            ast = _check_parse(eq.expression, schema, where, errors)
            if ast is None:
                continue
            if isinstance(ast, CompareNode):
                errors.append(f'{where}: expression must not contain a comparison operator.\nGot: {eq.expression!r}')
                continue
            _check_names(ast, eq.expression, where, variables, parameters, errors)

    if errors:
        raise ValueError('\n'.join(errors))


def _check_parse(
    expression: str,
    schema: MathSchema,
    context: str,
    errors: list[str],
) -> ArithNode | CompareNode | None:
    try:
        return parse_and_expand(expression, schema, context)
    except ValueError as e:
        errors.append(f'{context}: {e}')
        return None


def _check_where(
    text: str | None,
    context: str,
    errors: list[str],
) -> None:
    if text is None:
        return
    try:
        parse_where(text)
    except ValueError as e:
        errors.append(f'{context}: {e}')


def _check_names(
    node: ArithNode,
    expression: str,
    context: str,
    variables: set[str],
    parameters: set[str],
    errors: list[str],
) -> None:
    """Collect unknown names and unknown helpers under *node*."""
    if isinstance(node, NumberNode):
        return

    if isinstance(node, NameNode):
        if node.name not in variables and node.name not in parameters:
            errors.append(
                f"{context}: '{node.name}' not found in expression "
                f'{expression!r}.\n'
                f'  Variables:  {sorted(variables)}\n'
                f'  Parameters: {sorted(parameters)}\n'
                f"Check for typos, or ensure '{node.name}' is declared as "
                f'a variable or parameter.'
            )
        return

    if isinstance(node, UnaryOpNode):
        _check_names(node.operand, expression, context, variables, parameters, errors)
        return

    if isinstance(node, BinOpNode):
        _check_names(node.left, expression, context, variables, parameters, errors)
        _check_names(node.right, expression, context, variables, parameters, errors)
        return

    if isinstance(node, FuncCallNode):
        try:
            get_helper(node.name)
        except NameError as e:
            errors.append(f'{context}: {e}')
        for arg in node.args:
            _check_names(arg, expression, context, variables, parameters, errors)
        # Keyword-arg NameNodes are dimension names, not data references —
        # the evaluator passes them through as strings, so skip them here.
        for value in node.kwargs.values():
            if not isinstance(value, NameNode):
                _check_names(value, expression, context, variables, parameters, errors)
        return
