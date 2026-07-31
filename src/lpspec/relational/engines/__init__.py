"""The engines, and how a name becomes one.

Everything in here implements `relational.engine.Engine`; everything above it
in `relational/` is what an implementation answers to.

**The set is closed.** No `register_engine()`, for the same reason the sinks
have no `register_sink()`: an installed package that can change which engine
`lps.build` uses is hard rule 5's failure mode one level down — what a model
costs would depend on Python-side state the file cannot see. Adding an engine
is a pull request here.

Resolution is lazy. `duckdb` is an optional dependency and the import is the
expensive part, so naming an engine you do not use costs nothing.

**`LPSPEC_ENGINE` is the only way to choose**, and `lps.build` takes no engine
parameter. That is deliberate rather than minimal: the engines build the same
model integer for integer (`tests/test_engine_parity.py`), so the choice cannot
change the answer — only what computing it costs. `coords` belongs in the call
because it decides *what model is built*; an engine decides nothing, and a knob
that cannot change the answer does not belong in the signature that produces
one.

It also keeps the choice where it belongs. Which engine suits a machine is an
operational fact about that machine, and committing it into the code that
describes the math couples the two. An environment can say "run everything on
duckdb here" without touching a line.

This is not the session state hard rule 4 forbids: that rule is about
Python-side state changing what a file *means*, and nothing here can.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lpspec.relational.engine import Engine

#: name → ``module:attribute``. The default is first, and is the one every
#: published benchmark is measured on.
ENGINES: dict[str, str] = {
    'polars': 'lpspec.relational.engines.polars.executor:PolarsExecutor',
    'duckdb': 'lpspec.relational.engines.duck.executor:DuckExecutor',
}

DEFAULT_ENGINE = 'polars'

#: The only switch. Unset means `DEFAULT_ENGINE`.
ENV_VAR = 'LPSPEC_ENGINE'

#: What an engine needs installed, for an error naming the fix rather than the
#: missing module.
_EXTRA = {'duckdb': 'duckdb'}


def resolve(engine: str | None = None) -> type[Engine]:
    """The engine class to build with: *engine*, else `LPSPEC_ENGINE`, else the default.

    The argument exists for the tests and the benchmark harness, which need a
    named engine without an environment; nothing on the public path passes it.
    """
    import importlib

    from_env = False
    if engine is None:
        engine = os.environ.get(ENV_VAR) or DEFAULT_ENGINE
        from_env = engine != DEFAULT_ENGINE
    if engine not in ENGINES:
        known = ', '.join(repr(n) for n in ENGINES)
        # naming the *source* matters here: an unknown name in the environment
        # is a typo in a shell profile, and reads as a library bug otherwise
        where = f' (from {ENV_VAR})' if from_env else ''
        msg = f'unknown engine {engine!r}{where} — available: {known}'
        raise ValueError(msg)
    module, _, attribute = ENGINES[engine].partition(':')
    try:
        return getattr(importlib.import_module(module), attribute)
    except ImportError as exc:
        extra = _EXTRA.get(engine)
        if extra is None:
            raise
        msg = (
            f"the {engine!r} engine needs the '{extra}' package, which is not "
            f'installed. pip install "lpspec[{extra}]" — or use the default '
            f'engine, which needs nothing extra.'
        )
        raise ImportError(msg) from exc
