"""Expand ``piecewise:`` blocks into plain variables and constraints.

This is schema-level expansion (SPEC §12.4): a ``piecewise:`` block becomes
ordinary affine declarations *before* anything is built, so both backends —
eager and relational — receive identical schemas and stay differential-
testable. Formulations never enter the IR as expression nodes.

The λ convex-combination method is used because it is expansion-pure: it
needs only the breakpoint coordinate parameters themselves, no derived data
(no slopes, intercepts, or segment lengths). For a block

    piecewise:
      curve:
        over: bp
        links:
          - [power, power_bp]
          - [fuel * eff, fuel_bp, "<="]

with F = the union of the links' dims, it emits:

    variables:
      curve_lam(F, bp)  in [0, 1]
      curve_seg(F, bp)  binary                            (omitted when convex)
    constraints:
      curve_convexity(F):     sum(curve_lam, over=bp) == 1
      curve_pick(F):          sum(curve_seg, over=bp) == 1        (when not convex)
      curve_adjacency(F, bp): curve_lam <= curve_seg + shift(curve_seg, bp=1)
      curve_link0(F):         (power) == sum(curve_lam * power_bp, over=bp)
      curve_link1(F):         (fuel * eff) <= sum(curve_lam * fuel_bp, over=bp)

With adjacency, at most two *neighbouring* λ are nonzero, so the linked
expressions lie on the piecewise curve exactly. Without it (``convex:
true``), they range over the convex hull of the breakpoints — the correct
relaxation for convex/concave curves under optimisation pressure.

Link expressions must use the core language subset (their dims are inferred
through the lowering machinery), which they need anyway to run on both
backends.
"""

from __future__ import annotations

from linopy_yaml.expression_parser import CompareNode, parse_expression
from linopy_yaml.schema import MathSchema, PiecewiseDef


class PiecewiseExpansionError(ValueError):
    """A piecewise block references something that doesn't exist or collides."""


def expand_piecewise(schema: MathSchema) -> MathSchema:
    """Return *schema* with every ``piecewise:`` block expanded away."""
    if not schema.piecewise:
        return schema

    raw = schema.model_dump()
    for name, pw in schema.piecewise.items():
        frame = _validate_block(schema, name, pw)
        lam, seg = f"{name}_lam", f"{name}_seg"

        raw["variables"][lam] = {
            "foreach": [*frame, pw.over],
            "bounds": {"lower": 0, "upper": 1},
        }
        raw["constraints"][f"{name}_convexity"] = {
            "foreach": list(frame),
            "equations": [{"expression": f"sum({lam}, over={pw.over}) == 1"}],
        }
        for i, link in enumerate(pw.links):
            expr, values = link[0], link[1]
            sign = link[2] if len(link) == 3 else "=="
            raw["constraints"][f"{name}_link{i}"] = {
                "foreach": list(frame),
                "equations": [
                    {
                        "expression": (
                            f"({expr}) {sign} sum({lam} * {values}, over={pw.over})"
                        )
                    }
                ],
            }
        if not pw.convex:
            raw["variables"][seg] = {
                "foreach": [*frame, pw.over],
                "binary": True,
                "bounds": {},
            }
            raw["constraints"][f"{name}_pick"] = {
                "foreach": list(frame),
                "equations": [{"expression": f"sum({seg}, over={pw.over}) == 1"}],
            }
            raw["constraints"][f"{name}_adjacency"] = {
                "foreach": [*frame, pw.over],
                "equations": [
                    {"expression": f"{lam} <= {seg} + shift({seg}, {pw.over}=1)"}
                ],
            }

    raw["piecewise"] = {}
    return MathSchema(**raw)


def _validate_block(schema: MathSchema, name: str, pw: PiecewiseDef) -> tuple[str, ...]:
    """Check references and infer the frame (union of the links' dims)."""
    ctx = f"piecewise '{name}'"
    if pw.over not in schema.dimensions:
        raise PiecewiseExpansionError(
            f"{ctx}: over references undeclared dimension '{pw.over}'"
        )

    frame: list[str] = []
    for i, link in enumerate(pw.links):
        expr_text, values = link[0], link[1]
        if values not in schema.parameters:
            raise PiecewiseExpansionError(
                f"{ctx}: link {i} values references undeclared parameter '{values}'"
            )
        if pw.over not in schema.parameters[values].dims:
            raise PiecewiseExpansionError(
                f"{ctx}: link {i} values parameter '{values}' must carry dim "
                f"'{pw.over}' (has {schema.parameters[values].dims})"
            )
        for d in _expr_dims(schema, expr_text, f"{ctx} link {i}"):
            if d == pw.over:
                raise PiecewiseExpansionError(
                    f"{ctx}: link {i} expression already carries the breakpoint "
                    f"dim '{pw.over}'"
                )
            if d not in frame:
                frame.append(d)

    for emitted in (f"{name}_lam", f"{name}_seg"):
        if emitted in schema.variables:
            raise PiecewiseExpansionError(
                f"{ctx}: emitted variable '{emitted}' collides with a declared variable"
            )
    for i in range(len(pw.links)):
        if f"{name}_link{i}" in schema.constraints:
            raise PiecewiseExpansionError(
                f"{ctx}: emitted constraint '{name}_link{i}' collides with a "
                f"declared constraint"
            )
    for emitted in (f"{name}_convexity", f"{name}_pick", f"{name}_adjacency"):
        if emitted in schema.constraints:
            raise PiecewiseExpansionError(
                f"{ctx}: emitted constraint '{emitted}' collides with a declared "
                f"constraint"
            )
    return tuple(frame)


def _expr_dims(schema: MathSchema, text: str, ctx: str) -> frozenset[str]:
    """Dims of an affine link expression, via the lowering machinery."""
    from linopy_yaml.lowering import _dims_of, _lower_expr
    from linopy_yaml.relational.executor import RelationalBuildError

    ast = parse_expression(text)
    if isinstance(ast, CompareNode):
        raise PiecewiseExpansionError(
            f"{ctx}: link expressions must not contain a comparison, got {text!r}"
        )
    try:
        return _dims_of(_lower_expr(ast, schema, ctx), schema)
    except RelationalBuildError as exc:
        raise PiecewiseExpansionError(
            f"{ctx}: link expression {text!r} is not a core-subset affine "
            f"expression: {exc}"
        ) from exc
