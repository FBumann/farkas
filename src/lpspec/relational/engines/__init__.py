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

**`LPSPEC_ENGINE` sets the default for a process**, under an explicit `engine=`
which always wins. It exists because the only way to answer "is the other
engine worth it for *our* models" is to run a real workload on it, and editing
every call site to find out is a poor trade. It is a cost knob and nothing
more: the engines build the same model integer for integer
(`tests/test_engine_parity.py`), so this cannot change what a YAML file means —
only what building it costs. That is what keeps it clear of hard rule 4, which
is about Python-side state changing *meaning*.
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

#: Overrides `DEFAULT_ENGINE` for a process; an explicit `engine=` overrides it.
ENV_VAR = 'LPSPEC_ENGINE'

#: What an engine needs installed, for an error naming the fix rather than the
#: missing module.
_EXTRA = {'duckdb': 'duckdb'}


def resolve(engine: str | type[Engine] | None) -> type[Engine]:
    """An engine name — or a class, passed straight through — as a class.

    A class is accepted so a caller experimenting with an engine of their own
    is not forced through this table. It is not a plugin hook: nothing
    *discovers* such a class, so a model still cannot be built by an engine the
    calling program did not name.
    """
    import importlib

    from_env = False
    if engine is None:
        engine = os.environ.get(ENV_VAR) or DEFAULT_ENGINE
        from_env = engine != DEFAULT_ENGINE
    if not isinstance(engine, str):
        return engine
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
