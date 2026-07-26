"""Shared fixtures and schema helpers for farkas tests.

Everything here is linopy-free, so it loads on a bare install. On a bare
install (no [linopy] extra) the eager/oracle modules skip themselves: they
reach the oracle through ``tests.oracle``, whose ``importorskip`` guard fires
at collection. There is no list of filenames to keep in sync here — a module
that needs the extra says so by importing it. The differential harness lives
in ``tests.differential`` for the same reason: importing it *is* the guard.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml as pyyaml

from farkas.schema import MathSchema

EXAMPLES_DIR = Path(__file__).parent.parent / 'examples'

#: The dispatch model as a dict, for tests that need to mutate a declaration
#: rather than read a file. Deliberately the same math as
#: ``examples/dispatch.yaml`` so a reader who knows one knows the other; use
#: :func:`override` to vary it.
DISPATCH_MODEL: dict[str, Any] = {
    'dimensions': {'snapshot': {'dtype': 'int'}, 'generator': {'values': ['wind', 'gas']}},
    'parameters': {
        'p_max': {'dims': ['generator']},
        'cost': {'dims': ['generator']},
        'load': {'dims': ['snapshot']},
    },
    'variables': {'p': {'foreach': ['snapshot', 'generator'], 'bounds': {'lower': 0, 'upper': 'p_max'}}},
    'constraints': {
        'balance': {'foreach': ['snapshot'], 'equations': [{'expression': 'sum(p, over=generator) == load'}]}
    },
    'objectives': {'total': {'sense': 'minimize', 'equations': [{'expression': 'sum(p * cost, over=generator)'}]}},
}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        '--update-golden',
        action='store_true',
        default=False,
        help='rewrite committed golden output (examples/*.out) from this run instead of asserting on it',
    )


# ---------------------------------------------------------------------------
# building schemas to test against
# ---------------------------------------------------------------------------


def override(base: dict[str, Any], **patch: Any) -> dict[str, Any]:
    """A deep copy of ``base`` with dotted paths replaced.

    ``override(DISPATCH_MODEL, **{'variables.p.where': 'p_max > 0'})``. Missing
    intermediate keys are created, so this both edits an existing declaration
    and adds a new one — which is what makes a whole family of "the base model
    but for one thing" tests a one-liner each.
    """
    raw = copy.deepcopy(base)
    for dotted, value in patch.items():
        node = raw
        *parents, leaf = dotted.split('.')
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = value
    return raw


def schema_of(source: str | Path | dict[str, Any], **patch: Any) -> MathSchema:
    """A ``MathSchema`` from a YAML path, YAML text, or a raw dict.

    ``Path`` means a file, ``str`` means the YAML itself — the distinction is
    the type, never a guess about the content. ``**patch`` applies
    :func:`override` first, which is how a test says "this example, but with
    ``**`` in the objective".
    """
    raw = raw_of(source)
    return MathSchema(**(override(raw, **patch) if patch else raw))


def raw_of(source: str | Path | dict[str, Any]) -> dict[str, Any]:
    """The parsed mapping behind a path / YAML text / dict, unvalidated."""
    if isinstance(source, dict):
        return source
    text = source.read_text() if isinstance(source, Path) else source
    return pyyaml.safe_load(text)


def solve_lp_file(path: Path | str) -> float:
    """Objective HiGHS reaches reading the written LP file back from disk.

    The third opinion in a differential: ``solver_direct`` builds the model
    through the HiGHS API, this one round-trips it through text, and a sink
    that writes a wrong file is otherwise invisible. Lives here rather than in
    ``tests.differential`` because highspy is a core dependency — a bare
    install must still be able to check the LP sink.
    """
    import highspy

    h = highspy.Highs()
    h.setOptionValue('output_flag', False)
    h.readModel(str(path))
    h.run()
    assert h.getModelStatus() == highspy.HighsModelStatus.kOptimal
    return h.getInfo().objective_function_value


def resolved(text, schema):
    """Parse + expand + resolve — exactly what a backend receives.

    Tests that call `_lower_expr` or `evaluate_where` directly must go through
    this: a raw `parse_expression` result still holds NameNodes, and both
    backends now assert those never reach them (resolution.py).
    """
    from farkas.resolution import Namespace, expression_of

    return expression_of(text, schema, Namespace.of(schema), 't')


def resolved_where(text, schema):
    """Parse + resolve a where string."""
    from farkas.resolution import Namespace, where_of

    return where_of(text, Namespace.of(schema), 't')


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatch_yaml() -> Path:
    return EXAMPLES_DIR / 'dispatch.yaml'


@pytest.fixture
def dispatch_inputs():
    """Data for ``examples/dispatch.yaml``: distinct costs, so the optimal
    vertex is unique and primals are comparable across lanes."""
    rng = np.random.default_rng(3)
    n_s = 48
    p_max = pd.Series({'wind': 100.0, 'solar': 60.0, 'gas': 200.0})
    cost = pd.Series({'wind': 1.0, 'solar': 2.0, 'gas': 50.0})
    load = pd.Series(
        (rng.uniform(0.2, 0.8, n_s) * p_max.sum()).round(3),
        index=pd.RangeIndex(n_s, name='snapshot'),
    )
    data = {'p_max': p_max, 'load': load, 'cost': cost}
    coords = {'snapshot': pd.RangeIndex(n_s, name='snapshot')}
    return data, coords


@pytest.fixture
def transport_data():
    rng = np.random.default_rng(11)
    n_s, n_b, n_g, n_l = 24, 4, 9, 5
    buses = [f'b{i}' for i in range(n_b)]
    gens = pd.DataFrame(
        {
            'generator': [f'g{i}' for i in range(n_g)],
            # round-robin so every bus has local generation (keeps the data
            # feasible); the cost spread still makes cross-bus flows optimal
            'bus': [buses[i % n_b] for i in range(n_g)],
            'p_max': rng.uniform(80, 150, n_g).round(3),
            'cost': rng.uniform(5, 100, n_g).round(3),
        }
    )
    # ring topology plus one chord so every bus is reachable
    pairs = [(buses[i], buses[(i + 1) % n_b]) for i in range(n_b)] + [(buses[0], buses[2])]
    lines = pd.DataFrame(
        {
            'line': [f'l{i}' for i in range(n_l)],
            'from_bus': [a for a, _ in pairs],
            'to_bus': [b for _, b in pairs],
            'cap': rng.uniform(60, 120, n_l).round(3),
        }
    )
    # loads below each bus's local capacity — feasible even with zero flow
    local_cap = gens.groupby('bus')['p_max'].sum().reindex(buses).to_numpy()
    factors = rng.uniform(0.3, 0.8, (n_s, n_b))
    load = pd.DataFrame(
        {
            'snapshot': np.repeat(np.arange(n_s), n_b),
            'bus': buses * n_s,
            'value': (factors * local_cap).round(3).ravel(),
        }
    )
    return gens, lines, load
