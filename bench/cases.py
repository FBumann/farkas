"""The models we time, and the data that sizes them.

One case = one YAML model + a deterministic data generator + a size ladder.
Cases are chosen so each stresses a *different* SQL shape (ARCHITECTURE.md,
"read the verdict off the SQL"), not to cover the language:

``dispatch``   pointwise bounds + one ``sum`` — raw throughput, and the case
               where a dense eager broadcast is at its best, so our worst ratio.
``transport``  three ``group_sum`` joins per row — the mapping-table path, where
               the eager lane has to materialise a bus x generator product.

Data is generated once per (case, shape) into a cache directory and both arms
read the same parquet files, so no arm pays a generation cost and neither can
be measured against different numbers. Feasibility is by construction: every
bus can serve its own load without any flow, so a solve is never the thing that
fails at 3am in a benchmark.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

BENCH_DIR = Path(__file__).resolve().parent
MODELS = BENCH_DIR / 'models'
DEFAULT_CACHE = BENCH_DIR / '.cache'


@dataclass(frozen=True)
class Shape:
    """One rung of a size ladder: the dimension cardinalities of a run."""

    label: str
    sizes: dict[str, int]
    nominal_variables: int

    @property
    def key(self) -> str:
        return '-'.join(f'{k}{v}' for k, v in sorted(self.sizes.items()))


@dataclass(frozen=True)
class Case:
    name: str
    model: Path
    ladder: tuple[Shape, ...]
    write: Callable[[Shape, Path], dict[str, str]]
    eager_inputs: Callable[[dict[str, str]], tuple[dict[str, Any], dict[str, Any]]]

    def shape(self, label: str) -> Shape:
        for s in self.ladder:
            if s.label == label:
                return s
        known = ', '.join(s.label for s in self.ladder)
        raise KeyError(f"{self.name}: no size '{label}' (have: {known})")

    def data(self, shape: Shape, cache: Path = DEFAULT_CACHE) -> dict[str, str]:
        """Parquet paths for *shape*, generating them on first use."""
        out = cache / self.name / shape.key
        stamp = out / '.complete'
        if not stamp.exists():
            out.mkdir(parents=True, exist_ok=True)
            paths = self.write(shape, out)
            stamp.write_text('\n'.join(sorted(paths)))
            return paths
        return {p.stem: str(p) for p in sorted(out.glob('*.parquet'))}


def _seed(shape: Shape) -> np.random.Generator:
    """Same shape, same numbers — on any machine, in any arm, forever.

    ``hash()`` is salted per process, so it cannot be used here: the two arms
    run in different processes and must see byte-identical data.
    """
    digest = hashlib.blake2b(shape.key.encode(), digest_size=4).digest()
    return np.random.default_rng(int.from_bytes(digest, 'big'))


def _dump(frames: dict[str, pd.DataFrame], dest: Path) -> dict[str, str]:
    paths = {}
    for name, df in frames.items():
        path = (dest / f'{name}.parquet').absolute()
        df.to_parquet(path, index=False)
        paths[name] = str(path)
    return paths


# --------------------------------------------------------------------------
# dispatch


def _dispatch_data(shape: Shape, dest: Path) -> dict[str, str]:
    rng = _seed(shape)
    n_snap, n_gen = shape.sizes['snapshot'], shape.sizes['generator']
    gens = [f'g{i:05d}' for i in range(n_gen)]

    p_max = rng.uniform(50.0, 150.0, n_gen)
    cost = rng.uniform(10.0, 100.0, n_gen)
    # 60% +- 20% of the fleet: always feasible, never so slack that every
    # generator prices in at zero.
    load = p_max.sum() * 0.6 * (0.8 + 0.4 * rng.random(n_snap))

    return _dump(
        {
            'p_max': pd.DataFrame({'generator': gens, 'value': p_max}),
            'cost': pd.DataFrame({'generator': gens, 'value': cost}),
            'load': pd.DataFrame({'snapshot': np.arange(n_snap), 'value': load}),
            'generator': pd.DataFrame({'generator': gens}),
            'snapshot': pd.DataFrame({'snapshot': np.arange(n_snap)}),
        },
        dest,
    )


def _dispatch_eager(paths: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The same parquet files, as the linopy lane wants them.

    Reading them is the eager arm's own cost and is timed inside its build
    phase — that is how linopy is actually used, and pretending the data is
    already in memory would flatter it.
    """
    p_max = pd.read_parquet(paths['p_max']).set_index('generator')['value']
    cost = pd.read_parquet(paths['cost']).set_index('generator')['value']
    load = pd.read_parquet(paths['load']).set_index('snapshot')['value']
    data = {'p_max': p_max, 'cost': cost, 'load': load}
    coords = {
        'generator': pd.Index(p_max.index, name='generator'),
        'snapshot': pd.Index(load.index, name='snapshot'),
    }
    return data, coords


# --------------------------------------------------------------------------
# transport


def _transport_data(shape: Shape, dest: Path) -> dict[str, str]:
    rng = _seed(shape)
    n_snap = shape.sizes['snapshot']
    n_gen, n_bus, n_line = shape.sizes['generator'], shape.sizes['bus'], shape.sizes['line']

    buses = [f'b{i:04d}' for i in range(n_bus)]
    gens = [f'g{i:05d}' for i in range(n_gen)]
    gen_bus = [buses[i % n_bus] for i in range(n_gen)]  # every bus gets generation
    p_max = rng.uniform(50.0, 150.0, n_gen)

    # a ring, then chords: from != to by construction
    lines = [f'l{i:05d}' for i in range(n_line)]
    frm = [buses[i % n_bus] for i in range(n_line)]
    to = [buses[(i % n_bus + 1 + i // n_bus) % n_bus] for i in range(n_line)]

    # each bus is servable from its own generators alone, so the model is
    # feasible whatever the line capacities do
    own = pd.Series(p_max, index=gen_bus).groupby(level=0).sum().reindex(buses).to_numpy()
    load = own[None, :] * 0.5 * (0.8 + 0.4 * rng.random((n_snap, n_bus)))
    snaps = np.repeat(np.arange(n_snap), n_bus)

    return _dump(
        {
            'p_max': pd.DataFrame({'generator': gens, 'value': p_max}),
            'cost': pd.DataFrame({'generator': gens, 'value': rng.uniform(10.0, 100.0, n_gen)}),
            'cap': pd.DataFrame({'line': lines, 'value': rng.uniform(20.0, 80.0, n_line)}),
            'neg_cap': pd.DataFrame({'line': lines, 'value': -rng.uniform(20.0, 80.0, n_line)}),
            'load': pd.DataFrame({'snapshot': snaps, 'bus': buses * n_snap, 'value': load.ravel()}),
            'generator': pd.DataFrame({'generator': gens, 'bus': gen_bus}),
            'line': pd.DataFrame({'line': lines, 'from': frm, 'to': to}),
            'bus': pd.DataFrame({'bus': buses}),
            'snapshot': pd.DataFrame({'snapshot': np.arange(n_snap)}),
        },
        dest,
    )


def _transport_eager(paths: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    import xarray as xr

    gens = pd.read_parquet(paths['generator'])
    lines = pd.read_parquet(paths['line'])
    load = pd.read_parquet(paths['load'])
    data = {
        'p_max': pd.read_parquet(paths['p_max']).set_index('generator')['value'],
        'cost': pd.read_parquet(paths['cost']).set_index('generator')['value'],
        'cap': pd.read_parquet(paths['cap']).set_index('line')['value'],
        'neg_cap': pd.read_parquet(paths['neg_cap']).set_index('line')['value'],
        'load': xr.DataArray.from_series(load.set_index(['snapshot', 'bus'])['value']),
    }
    coords = {
        'snapshot': pd.Index(sorted(load['snapshot'].unique()), name='snapshot'),
        'generator': gens,
        'bus': pd.Index(pd.read_parquet(paths['bus'])['bus'], name='bus'),
        'line': lines,
    }
    return data, coords


# --------------------------------------------------------------------------


def _ladder(sizes: dict[str, int], snapshots: Sequence[int], per_snapshot: int) -> tuple[Shape, ...]:
    labels = ('xs', 's', 'm', 'l', 'xl')
    return tuple(
        Shape(labels[i], {**sizes, 'snapshot': n}, n * per_snapshot) for i, n in enumerate(snapshots) if i < len(labels)
    )


CASES: dict[str, Case] = {
    'dispatch': Case(
        name='dispatch',
        model=MODELS / 'dispatch.yaml',
        # 100 generators: 1e4 / 1e5 / 1e6 / 1e7 variables
        ladder=_ladder({'generator': 100}, (100, 1_000, 10_000, 100_000), per_snapshot=100),
        write=_dispatch_data,
        eager_inputs=_dispatch_eager,
    ),
    'transport': Case(
        name='transport',
        model=MODELS / 'transport.yaml',
        # 100 generators + 40 lines per snapshot
        ladder=_ladder({'generator': 100, 'bus': 20, 'line': 40}, (70, 700, 7_000, 70_000), per_snapshot=140),
        write=_transport_data,
        eager_inputs=_transport_eager,
    ),
}
