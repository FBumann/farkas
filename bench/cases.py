"""The models we time, and the data that sizes them.

One case = one YAML model + a deterministic data generator + a size ladder.
Cases are chosen so each stresses a *different* SQL shape (ARCHITECTURE.md,
"read the verdict off the SQL"), not to cover the language:

``dispatch``   pointwise bounds + one ``sum`` — raw throughput, and the case
               where a dense eager broadcast is at its best, so our worst ratio.
               Its ``where`` is declared but *vacuous*, which is a measurement
               in itself: the engine pays for a mask that removes nothing.
``nodal``      dispatch over (snapshot, node, tech) where a technology only
               exists at the nodes it is installed at — the sparsity every real
               multi-node model has, and the one axis where the two lanes do
               different *amounts* of work rather than the same work in a
               different order.
``transport``  three ``group_sum`` joins per row — the mapping-table path, where
               the eager lane has to materialise a bus x generator product.

Data is generated once per (case, shape) into a cache directory and both arms
read the same parquet files, so no arm pays a generation cost and neither can
be measured against different numbers. Feasibility is by construction — every
bus serves its own load with no flow, and ``sparse`` sizes its load against the
tightest snapshot — so a solve never fails for a reason the harness invented.
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
    """One rung of a ladder: the dimension cardinalities, and how much survives.

    ``density`` is the fraction of the coordinate product a case's mask keeps.
    It is a rung axis rather than a case, because sparsity is the one place the
    two representations of a mask differ in kind — row absence relationally,
    NaN-padding in a dense array — so it has to be swept, not sampled once.
    Cases with no mask leave it at 1.0.
    """

    label: str
    sizes: dict[str, int]
    nominal_variables: int
    density: float = 1.0

    @property
    def key(self) -> str:
        dims = '-'.join(f'{k}{v}' for k, v in sorted(self.sizes.items()))
        return dims if self.density == 1.0 else f'{dims}-d{self.density:g}'


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
# nodal — a technology portfolio per node, which is where real sparsity comes from

#: Technologies a system might have. Which ones a given node *has* is the mask.
TECHNOLOGIES = (
    'onwind',
    'offwind',
    'solar',
    'hydro',
    'ror',
    'biomass',
    'geothermal',
    'ccgt',
    'ocgt',
    'coal',
    'nuclear',
    'oil',
)


def _portfolios(rng: np.random.Generator, n_node: int, n_tech: int, density: float) -> np.ndarray:
    """Which (node, tech) pairs exist — a boolean node x tech matrix.

    Every node gets at least one technology, or its demand cannot be met and
    the parity gate has no two objectives to compare. The rest are drawn to hit
    the requested density; what is actually achieved is reported, never assumed.
    """
    per_node = max(1, round(density * n_tech))
    installed = np.zeros((n_node, n_tech), dtype=bool)
    for i in range(n_node):
        installed[i, rng.choice(n_tech, size=per_node, replace=False)] = True
    return installed


def _nodal_data(shape: Shape, dest: Path) -> dict[str, str]:
    rng = _seed(shape)
    n_snap, n_node = shape.sizes['snapshot'], shape.sizes['node']
    techs = list(TECHNOLOGIES[: shape.sizes['tech']])
    nodes = [f'n{i:04d}' for i in range(n_node)]

    installed = _portfolios(rng, n_node, len(techs), shape.density)
    capacity = installed * rng.uniform(200.0, 800.0, (n_node, len(techs)))
    cost = rng.uniform(10.0, 100.0, len(techs))

    # every node meets its own demand from its own portfolio: this model has no
    # transmission, so feasibility must not depend on the draw
    at_node = capacity.sum(axis=1)
    demand = at_node[None, :] * 0.5 * (0.8 + 0.4 * rng.random((n_snap, n_node)))

    return _dump(
        {
            # only installed pairs are stored — the tidy table *is* the sparsity
            'installed': pd.DataFrame(
                {
                    'node': np.repeat(nodes, len(techs))[installed.reshape(-1)],
                    'tech': np.tile(techs, n_node)[installed.reshape(-1)],
                    'value': capacity.reshape(-1)[installed.reshape(-1)],
                }
            ),
            'cost': pd.DataFrame({'tech': techs, 'value': cost}),
            'demand': pd.DataFrame(
                {
                    'snapshot': np.repeat(np.arange(n_snap), n_node),
                    'node': nodes * n_snap,
                    'value': demand.reshape(-1),
                }
            ),
            'node': pd.DataFrame({'node': nodes}),
            'tech': pd.DataFrame({'tech': techs}),
            'snapshot': pd.DataFrame({'snapshot': np.arange(n_snap)}),
        },
        dest,
    )


def _nodal_eager(paths: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    import xarray as xr

    nodes = pd.read_parquet(paths['node'])['node']
    techs = pd.read_parquet(paths['tech'])['tech']
    cost = pd.read_parquet(paths['cost']).set_index('tech')['value']
    demand = pd.read_parquet(paths['demand'])
    # the eager lane cannot hold an absent pair: reindexing over the full
    # node x tech product is what turns structural sparsity into NaN padding,
    # and doing it here rather than pretending otherwise is the point of the case
    installed = (
        pd.read_parquet(paths['installed'])
        .set_index(['node', 'tech'])['value']
        .unstack()
        .reindex(index=nodes, columns=techs)
        .fillna(0.0)
    )
    data = {
        'installed': installed,
        'cost': cost,
        'demand': xr.DataArray.from_series(demand.set_index(['snapshot', 'node'])['value']),
    }
    coords = {
        'snapshot': pd.Index(sorted(demand['snapshot'].unique()), name='snapshot'),
        'node': pd.Index(nodes, name='node'),
        'tech': pd.Index(techs, name='tech'),
    }
    return data, coords


# --------------------------------------------------------------------------
# sector


#: What each technology's output arrives as. One carrier per technology, which
#: is what makes the tech x carrier map sparser than the portfolio above it.
CARRIERS = ('electricity', 'heat', 'hydrogen', 'gas', 'transport')


def _sector_data(shape: Shape, dest: Path) -> dict[str, str]:
    rng = _seed(shape)
    n_snap, n_node = shape.sizes['snapshot'], shape.sizes['node']
    techs = list(TECHNOLOGIES[: shape.sizes['tech']])
    carriers = list(CARRIERS[: shape.sizes['carrier']])
    nodes = [f'n{i:04d}' for i in range(n_node)]

    installed = _portfolios(rng, n_node, len(techs), shape.density)
    capacity = installed * rng.uniform(200.0, 800.0, (n_node, len(techs)))
    serves = rng.integers(0, len(carriers), len(techs))
    efficiency = rng.uniform(0.3, 0.95, len(techs))

    # what a node can actually deliver into a carrier. Demand exists only where
    # that is nonzero, which is what keeps the model feasible on any draw and
    # what makes the demand table sparse in (node, carrier) while dense in time
    reachable = np.zeros((n_node, len(carriers)))
    for t, c in enumerate(serves):
        reachable[:, c] += capacity[:, t] * efficiency[t]
    served = reachable > 0
    demand = reachable[None, :, :] * 0.6 * (0.8 + 0.4 * rng.random((n_snap, n_node, len(carriers))))
    live = np.broadcast_to(served, demand.shape).reshape(-1)

    return _dump(
        {
            'installed': pd.DataFrame(
                {
                    'node': np.repeat(nodes, len(techs))[installed.reshape(-1)],
                    'tech': np.tile(techs, n_node)[installed.reshape(-1)],
                    'value': capacity.reshape(-1)[installed.reshape(-1)],
                }
            ),
            'produces': pd.DataFrame({'tech': techs, 'carrier': [carriers[c] for c in serves], 'value': efficiency}),
            'cost': pd.DataFrame({'tech': techs, 'value': rng.uniform(10.0, 100.0, len(techs))}),
            'demand': pd.DataFrame(
                {
                    'snapshot': np.repeat(np.arange(n_snap), n_node * len(carriers))[live],
                    'node': np.tile(np.repeat(nodes, len(carriers)), n_snap)[live],
                    'carrier': np.array(carriers * (n_snap * n_node))[live],
                    'value': demand.reshape(-1)[live],
                }
            ),
            'node': pd.DataFrame({'node': nodes}),
            'tech': pd.DataFrame({'tech': techs}),
            'carrier': pd.DataFrame({'carrier': carriers}),
            'snapshot': pd.DataFrame({'snapshot': np.arange(n_snap)}),
        },
        dest,
    )


def _sector_eager(paths: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    import xarray as xr

    nodes = pd.read_parquet(paths['node'])['node']
    techs = pd.read_parquet(paths['tech'])['tech']
    carriers = pd.read_parquet(paths['carrier'])['carrier']
    demand = pd.read_parquet(paths['demand'])
    # both sparse tables are reindexed over their full product here: the eager
    # lane has nowhere else to put an absent pair, and doing it in the open is
    # what makes the arms comparable
    installed = (
        pd.read_parquet(paths['installed'])
        .set_index(['node', 'tech'])['value']
        .unstack()
        .reindex(index=nodes, columns=techs)
        .fillna(0.0)
    )
    produces = (
        pd.read_parquet(paths['produces'])
        .set_index(['tech', 'carrier'])['value']
        .unstack()
        .reindex(index=techs, columns=carriers)
        .fillna(0.0)
    )
    data = {
        'installed': installed,
        'produces': produces,
        'cost': pd.read_parquet(paths['cost']).set_index('tech')['value'],
        'demand': xr.DataArray.from_series(demand.set_index(['snapshot', 'node', 'carrier'])['value']),
    }
    coords = {
        'snapshot': pd.Index(sorted(demand['snapshot'].unique()), name='snapshot'),
        'node': pd.Index(nodes, name='node'),
        'tech': pd.Index(techs, name='tech'),
        'carrier': pd.Index(carriers, name='carrier'),
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
# fleet — many declarations rather than one large one


#: The variables `fleet` declares. Named here rather than inline because the
#: model file and the eager arm both have to agree on them, and a mismatch
#: would show up as a parity failure rather than as the typo it is.
FLEET_VARIABLES = (
    'p', 'p_up', 'p_down', 'reserve_up', 'reserve_down', 'charge',
    'discharge', 'soc', 'spill', 'curtail', 'import_', 'export_',
)  # fmt: skip


def _fleet_data(shape: Shape, dest: Path) -> dict[str, str]:
    rng = _seed(shape)
    n_snap, n_unit = shape.sizes['snapshot'], shape.sizes['unit']
    units = [f'u{i:05d}' for i in range(n_unit)]

    p_max = rng.uniform(50.0, 150.0, n_unit)
    # the balance can be met three ways and all three are priced, so the
    # optimum is a choice rather than "take the free one"
    demand = p_max.sum() * 0.6 * (0.8 + 0.4 * rng.random(n_snap))

    return _dump(
        {
            'p_max': pd.DataFrame({'unit': units, 'value': p_max}),
            'cost': pd.DataFrame({'unit': units, 'value': rng.uniform(10.0, 100.0, n_unit)}),
            'demand': pd.DataFrame({'snapshot': np.arange(n_snap), 'value': demand}),
            'unit': pd.DataFrame({'unit': units}),
            'snapshot': pd.DataFrame({'snapshot': np.arange(n_snap)}),
        },
        dest,
    )


def _fleet_eager(paths: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    p_max = pd.read_parquet(paths['p_max']).set_index('unit')['value']
    cost = pd.read_parquet(paths['cost']).set_index('unit')['value']
    demand = pd.read_parquet(paths['demand']).set_index('snapshot')['value']
    data = {'p_max': p_max, 'cost': cost, 'demand': demand}
    coords = {
        'unit': pd.Index(p_max.index, name='unit'),
        'snapshot': pd.Index(demand.index, name='snapshot'),
    }
    return data, coords


# --------------------------------------------------------------------------
# profiled — the one case whose input is the same order as the model


def _profiled_data(shape: Shape, dest: Path) -> dict[str, str]:
    rng = _seed(shape)
    n_snap, n_node = shape.sizes['snapshot'], shape.sizes['node']
    techs = list(TECHNOLOGIES[: shape.sizes['tech']])
    n_tech = len(techs)
    nodes = [f'n{i:04d}' for i in range(n_node)]

    capacity = rng.uniform(200.0, 800.0, (n_node, n_tech))
    # the point of the case: an availability factor per (snapshot, node, tech),
    # so `availability` has one row per variable rather than per coordinate of
    # some smaller product. Never zero — this case carries no mask, and a zero
    # upper bound would be sparsity by the back door.
    availability = capacity[None, :, :] * (0.2 + 0.8 * rng.random((n_snap, n_node, n_tech)))
    # half of what is available in *that* snapshot: feasible on every draw
    # without depending on a profile that happens to be high somewhere
    demand = availability.sum(axis=2) * 0.5

    return _dump(
        {
            'availability': pd.DataFrame(
                {
                    'snapshot': np.repeat(np.arange(n_snap), n_node * n_tech),
                    # categorical, unlike the other generators: at the `l` rung
                    # this frame is 12M rows, and two object columns of repeated
                    # labels would cost more to build than the model does. The
                    # parquet is dictionary-encoded either way, so nothing about
                    # what the arms *read* changes.
                    'node': pd.Categorical.from_codes(
                        np.tile(np.repeat(np.arange(n_node), n_tech), n_snap), categories=pd.Index(nodes)
                    ),
                    'tech': pd.Categorical.from_codes(
                        np.tile(np.arange(n_tech), n_snap * n_node), categories=pd.Index(techs)
                    ),
                    'value': availability.reshape(-1),
                }
            ),
            'cost': pd.DataFrame({'tech': techs, 'value': rng.uniform(10.0, 100.0, n_tech)}),
            'demand': pd.DataFrame(
                {
                    'snapshot': np.repeat(np.arange(n_snap), n_node),
                    'node': nodes * n_snap,
                    'value': demand.reshape(-1),
                }
            ),
            'node': pd.DataFrame({'node': nodes}),
            'tech': pd.DataFrame({'tech': techs}),
            'snapshot': pd.DataFrame({'snapshot': np.arange(n_snap)}),
        },
        dest,
    )


def _profiled_eager(paths: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    import xarray as xr

    nodes = pd.read_parquet(paths['node'])['node']
    techs = pd.read_parquet(paths['tech'])['tech']
    snapshots = pd.read_parquet(paths['snapshot'])['snapshot']
    demand = pd.read_parquet(paths['demand'])

    # This is the eager lane at its best, and the case exists to let it be:
    # the file already carries the layout xarray wants, so it reads and
    # reshapes rather than aligning. Routing it through `from_series` like the
    # sparse cases would time a sort the *harness* imposed, not one the shape
    # requires, and would make the comparison dishonest in our favour. Order is
    # load-bearing — the generator writes C-order (snapshot, node, tech) — so a
    # permuted file would change the objective and the parity gate would kill
    # the run before anything is timed.
    values = pd.read_parquet(paths['availability'])['value'].to_numpy()
    availability = xr.DataArray(
        values.reshape(len(snapshots), len(nodes), len(techs)),
        coords={'snapshot': snapshots.to_numpy(), 'node': nodes.to_numpy(), 'tech': techs.to_numpy()},
        dims=['snapshot', 'node', 'tech'],
    )

    data = {
        'availability': availability,
        'cost': pd.read_parquet(paths['cost']).set_index('tech')['value'],
        'demand': xr.DataArray.from_series(demand.set_index(['snapshot', 'node'])['value']),
    }
    coords = {
        'snapshot': pd.Index(snapshots, name='snapshot'),
        'node': pd.Index(nodes, name='node'),
        'tech': pd.Index(techs, name='tech'),
    }
    return data, coords


# --------------------------------------------------------------------------


def _ladder(
    sizes: dict[str, int],
    snapshots: Sequence[int],
    per_snapshot: int,
    density: float = 1.0,
) -> tuple[Shape, ...]:
    labels = ('xs', 's', 'm', 'l', 'xl', '2xl')
    return tuple(
        Shape(labels[i], {**sizes, 'snapshot': n}, n * per_snapshot, density)
        for i, n in enumerate(snapshots)
        if i < len(labels)
    )


def _density_sweep(
    sizes: dict[str, int], snapshots: int, per_snapshot: int, densities: Sequence[float]
) -> tuple[Shape, ...]:
    """One model size, several mask densities — rungs named ``d100``/``d30``/…

    Held at one size on purpose: sweeping both axes at once would leave no way
    to tell a density effect from a size effect.
    """
    return tuple(
        Shape(f'd{round(d * 100):02d}', {**sizes, 'snapshot': snapshots}, snapshots * per_snapshot, d)
        for d in densities
    )


CASES: dict[str, Case] = {
    'dispatch': Case(
        name='dispatch',
        model=MODELS / 'dispatch.yaml',
        # 100 generators: 1e4 / 1e5 / 1e6 / 1e7 / 4e7 variables.
        # `xl` is not just one more rung: below it every arm fits in RAM, so
        # the ladder measures throughput and says nothing about the invariant
        # the architecture exists for. It is the first rung where a budgeted
        # engine and an unbudgeted one visibly part company.
        # `2xl` (1.2e8) is the capability rung: docs/benchmarks.md claims a
        # model whose dense build cannot fit on the machine still streams out
        # under the budget, and a rung nothing else survives is the only way to
        # keep testing that claim rather than restating it.
        ladder=_ladder({'generator': 100}, (100, 1_000, 10_000, 100_000, 400_000, 1_200_000), per_snapshot=100),
        write=_dispatch_data,
        eager_inputs=_dispatch_eager,
    ),
    'fleet': Case(
        name='fleet',
        model=MODELS / 'fleet.yaml',
        # 12 variables and 7 constraints over (snapshot, unit), so the rung
        # labels count the *total* across declarations and stay comparable
        # with the rest of the ladder. `per_snapshot` is 12 declarations x 50
        # units; what this case varies is how many declarations that total is
        # spread over, and nothing else.
        ladder=_ladder({'unit': 50}, (20, 200, 2_000, 20_000), per_snapshot=600),
        write=_fleet_data,
        eager_inputs=_fleet_eager,
    ),
    'nodal': Case(
        name='nodal',
        model=MODELS / 'nodal.yaml',
        # 50 nodes x 12 technologies = 600 coordinates per snapshot, of which 3
        # per node are installed. `nominal_variables` is the *full* product;
        # what survives is measured, never assumed (see `live` in the report).
        # The density sweep is 12 / 6 / 3 / 1 technologies per node.
        ladder=(
            *_ladder({'node': 50, 'tech': 12}, (20, 200, 2_000, 20_000), per_snapshot=600, density=0.25),
            *_density_sweep({'node': 50, 'tech': 12}, 2_000, 600, (1.0, 0.5, 0.25, 0.083)),
        ),
        write=_nodal_data,
        eager_inputs=_nodal_eager,
    ),
    'sector': Case(
        name='sector',
        model=MODELS / 'sector.yaml',
        # 50 nodes x 12 technologies at 8% installed, crossed with 5 dense
        # carriers. `p` is sparse in (node, tech); `shed` and the balance are
        # dense in (node, carrier); the objective spans both.
        ladder=_ladder(
            {'node': 50, 'tech': 12, 'carrier': 5},
            (20, 200, 2_000, 20_000),
            per_snapshot=850,
            density=0.083,
        ),
        write=_sector_data,
        eager_inputs=_sector_eager,
    ),
    'profiled': Case(
        name='profiled',
        model=MODELS / 'profiled.yaml',
        # `nodal`'s ladder with no mask: 50 nodes x 12 technologies, all
        # installed. Held at those cardinalities on purpose — the two cases then
        # differ in exactly one thing, whether the parameters span the variable
        # product or a subset of it, so the rungs can be read against each other.
        # At `l` that is a 12M-row availability table against a 12M coordinate
        # product, which is where the "I/O is noise" claim gets tested.
        ladder=_ladder({'node': 50, 'tech': 12}, (20, 200, 2_000, 20_000), per_snapshot=600),
        write=_profiled_data,
        eager_inputs=_profiled_eager,
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
