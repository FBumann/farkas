"""What "the build" means, for the harnesses that can share a definition of it.

Two harnesses ask different questions (bench/README.md) with different metrics:
``bench/run.py`` spawns a process per measurement and reads ``ru_maxrss``;
``bench/regressions/`` runs memray in a forked interpreter, and CodSpeed
measures those same tests in CI. None of them can share a *measurement*.

**How much they share is uneven, and worth stating exactly.**
``bench/regressions/`` — under either instrument — calls the verbs below, so
the memray gate and the CodSpeed report cannot drift from each other. The
published ladder shares only :func:`split_sources`: ``_run_case.py`` marks a
phase clock between the build and the hand-off, so its copy of those two calls
is inline by necessity rather than by neglect. Read a ladder number and a
regression number as measuring the same *thing*, not as produced by the same
line of code.

Kept lpspec-free at import time on purpose: the library is imported inside the
verbs, not at module scope. ``bench/regressions/`` sends these to a fresh
process per pass and charges it for the import; ``bench/_run_case.py`` marks
the import as its own phase. Neither works if merely importing this module has
already paid for it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from bench.cases import CASES

if TYPE_CHECKING:
    from bench.cases import Case


def split_sources(case: Case, paths: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Parameters from dimension index tables, by what the model declares.

    Harness bookkeeping, and every caller runs it outside its measured region:
    it re-parses the YAML only because the *runner*, not lpspec, decides which
    parquet file is which. The linopy arm has no counterpart.
    """
    import yaml as pyyaml

    schema = pyyaml.safe_load(case.model.read_text())
    params = set(schema.get('parameters', {}))
    dims = set(schema.get('dimensions', {}))
    # A path the model declares nothing for would otherwise be dropped in
    # silence, and the case measured building a model that never saw it. The
    # way this happens is a stale parquet in the case's cache directory: the
    # generator's output is globbed on a cache hit, so a file an older
    # generator wrote outlives the declaration it was written for.
    undeclared = sorted(set(paths) - params - dims)
    if undeclared:
        raise ValueError(
            f'{case.name}: {undeclared} declared as neither parameter nor dimension in '
            f'{case.model} — the build would not see it. Stale files under bench/.cache/?'
        )
    return (
        {k: v for k, v in paths.items() if k in params},
        {k: v for k, v in paths.items() if k in dims},
    )


def build_and_write(case_name: str, sources: dict[str, str], coords: dict[str, str]) -> int:
    """Build the model and stream it to an LP file; return the column count.

    Top-level and picklable on purpose: ``bench/regressions/`` sends this to a
    fresh process per pass. Data paths are resolved by the caller so that
    generating the parquet — which is neither lpspec's work nor stable across
    machines — stays outside every measurement.
    """
    import lpspec as lps

    with (
        tempfile.TemporaryDirectory(prefix='lpspec-bench-') as tmp,
        lps.build(CASES[case_name].model, sources, coords=coords) as ex,
    ):
        ex.write(Path(tmp) / 'model.lp')
        return ex._tables().column_count


def build_and_hand_over(case_name: str, sources: dict[str, str], coords: dict[str, str]) -> int:
    """Build the model and stream it into HiGHS; return the column count.

    The sibling of :func:`build_and_write`, and the one that matches what most
    callers do. ``run()`` is deliberately never called: the simplex is HiGHS's
    work whoever filled the model, and a regression suite that included it would
    be watching a number nothing in this repository can move.
    """
    import lpspec as lps
    from lpspec.relational.sinks.solvers.highs import build_highs

    with lps.build(CASES[case_name].model, sources, coords=coords) as ex:
        tables = ex._tables()
        _handle = build_highs(tables)
        return tables.column_count
