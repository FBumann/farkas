"""Shared fixtures for linopy_yaml tests.

On a bare install (no [compat] extra) the compat/oracle modules skip
themselves: they reach the oracle through ``tests.oracle``, whose
``importorskip`` guard fires at collection. There is no list of filenames to
keep in sync here — a module that needs the extra says so by importing it.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

EXAMPLES_DIR = Path(__file__).parent.parent / 'examples'


def resolved(text, schema):
    """Parse + expand + resolve — exactly what a backend receives.

    Tests that call `_lower_expr` or `evaluate_where` directly must go through
    this: a raw `parse_expression` result still holds NameNodes, and both
    backends now assert those never reach them (resolution.py).
    """
    from linopy_yaml.resolution import Namespace, expression_of

    return expression_of(text, schema, Namespace.of(schema), 't')


def resolved_where(text, schema):
    """Parse + resolve a where string."""
    from linopy_yaml.resolution import Namespace, where_of

    return where_of(text, Namespace.of(schema), 't')


@pytest.fixture
def dispatch_yaml() -> Path:
    return EXAMPLES_DIR / 'dispatch.yaml'


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
