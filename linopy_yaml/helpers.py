"""The closed set of built-in helper names.

The set is closed: there is no Python registry. Both lanes therefore accept
exactly the same language, which is what makes the differential tests a
meaningful oracle (ARCHITECTURE.md, hard rule 3). Compositions of these
built-ins belong in ``macros:``; math the language cannot say belongs in a
declared ``escape:`` island (#38), not in a helper that reads like a
built-in on the page.

This module is the *language* side of a helper — its name, and nothing else.
It is imported by the linopy-free lane (``validation.py``, ``lowering.py``),
so it must stay dependency-free. The eager evaluations live with the eager
backend (``builder.py``); the relational ones are lowering cases and SQL.
"""

from __future__ import annotations

BUILTIN_NAMES = frozenset({'sum', 'roll', 'shift', 'group_sum'})


def unknown_helper_message(name: str) -> str:
    """The one wording for "that is not a helper", shared by both lanes."""
    return (
        f"Unknown helper function '{name}'.\n"
        f'Available: {sorted(BUILTIN_NAMES)}\n'
        f"Define '{name}' as a macro under 'macros:' if it composes built-ins; "
        f'if the math is not sayable in the language, use a declared escape.'
    )
