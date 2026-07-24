"""Backend selection: relational when eligible, eager otherwise (SPEC §12.8).

The relational backend is an optimization lane, not a parallel universe — it
must fall back. A schema is relational-eligible iff it lowers to the IR;
anything outside the streaming subset routes to the feature-complete eager
builder with a stated reason.

Eligibility is decided by *attempting the lowering*, so the answer can never
drift from what the backend actually supports: a new lowering capability
automatically widens eligibility, a new rejection automatically narrows it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from linopy_yaml.relational.executor import RelationalBuildError
from linopy_yaml.schema import MathSchema


@dataclass(frozen=True)
class BackendChoice:
    """The router's decision. ``reason`` is set only when falling back."""

    backend: Literal["relational", "eager"]
    reason: str | None = None


def relational_eligibility(schema: MathSchema) -> str | None:
    """Return ``None`` if *schema* lowers to the relational IR, else the reason.

    The reason is the first lowering error, verbatim — it names the construct
    and its context (e.g. ``"constraint 'soc_balance': helper 'my_helper' is
    not supported by the relational backend …"``).
    """
    from linopy_yaml.lowering import lower_program

    try:
        lower_program(schema)
    except RelationalBuildError as exc:
        return str(exc)
    return None


def select_backend(schema: MathSchema) -> BackendChoice:
    """Choose the backend for *schema*: relational when eligible, else eager."""
    reason = relational_eligibility(schema)
    if reason is None:
        return BackendChoice("relational")
    return BackendChoice("eager", reason)
