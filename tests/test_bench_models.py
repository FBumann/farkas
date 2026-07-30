"""The benchmark corpus still loads, and still builds.

`bench/` has its own models, and until #343 nothing checked them. The benchmark
workflow runs only on a `trigger:bench` label — asked for, never guessed, which
is right for a job that costs a runner — so no gate ever opened these files.
#329 removed `equations:`, `examples/` was migrated, and all six bench models
silently stopped loading. The README's headline numbers come from this suite, so
they were unreproducible from a clean checkout and nothing failed.

Two gates, because loading does not imply building. `check()` needs no data at
all and holds even on the bare install. The build needs `bench.cases` to
generate a rung, and that imports pandas — which the bare install does not
have — so it skips there and runs everywhere else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import lpspec as lps

MODELS = Path(__file__).resolve().parent.parent / 'bench' / 'models'


def _models() -> list[Path]:
    return sorted(MODELS.glob('*.yaml'))


#: Case names are the model stems, so this needs no import from `bench`, which
#: is what lets the load gate below run on the bare install.
CASE_NAMES = [p.stem for p in _models()]


@pytest.mark.parametrize('model', _models(), ids=lambda p: p.stem)
def test_a_bench_model_loads(model: Path):
    """Every language change has to migrate this corpus too, or fail here."""
    lps.check(model)


def test_the_corpus_is_not_empty():
    """A guard on the guard: the parametrised tests pass vacuously if the glob
    stops matching — a rename of `bench/models/` would silently retire them.
    """
    assert len(_models()) >= 6, f'expected the bench corpus to be found; got {CASE_NAMES}'


@pytest.mark.parametrize('case', CASE_NAMES)
def test_a_bench_case_builds_on_the_smallest_rung(case: str, tmp_path: Path):
    """Loading is not building, which is why both gates exist: `sector` passed
    `check()` and then died in the executor on a presence key a broadcast had
    widened (#345). The smallest rung costs milliseconds, so that difference is
    worth holding here rather than on a labelled runner.
    """
    bench_cases = pytest.importorskip('bench.cases', reason='needs pandas; the bare install has none')

    spec = bench_cases.CASES[case]
    sources = spec.write(spec.shape('xs'), tmp_path)
    with lps.build(spec.model, sources) as ex:
        assert ex is not None


def test_every_model_backs_a_case():
    """A model nothing runs, or a case whose model was renamed away. The two
    lists are matched by stem, which is what the parametrisation above assumes.
    """
    bench_cases = pytest.importorskip('bench.cases', reason='needs pandas; the bare install has none')
    assert sorted(bench_cases.CASES) == CASE_NAMES
