# Review: `feat!: rebuild the relational engine on polars` (#189)

**+2,385 / −2,280 across 50 files.** Verified locally against a worktree of `polars-engine` (rebased on current `main`, `2a195fa`) with `main` as a side-by-side control.

**Baseline health:** `380 passed, 1 xfailed`; `ruff check` clean; `pyrefly` 0 errors (2 suppressed). The structural claim in the PR body holds up — `plan.py`, `lowering.py`, the parsers and the linopy lane really are untouched, and the rewrite is confined to `compiler.py` / `executor.py` / `sinks/`. `plan.py` as a frozen engine-agnostic contract did its job.

---

## 🔴 Blocking: silently wrong models from `group_sum` over a broadcast dim

`compiler.py:374` — `_group_fragment` carries `p.keyed` through unchanged:

```python
return TermFragment((*keep, g.into), frame, p.is_term, p.keyed, _relabel(...))
```

`keyed` means "at most one row per `(dims…, var_label)`", and `executor.py:735` `_needs_aggregate` uses it to **skip the `group_by('row','col').agg(sum)`** that `main` always runs. That skip is sound when `over ∈ label_dims` (the variable's own `foreach` — the transport case). It is unsound when `over` arrived by *broadcast*: two labels of `over` mapping into the same group then produce two rows with the same `(keep, into, var_label)`, and the dedup that would have merged them has been skipped.

Reachable from ordinary YAML — a variable indexed by fewer dims than the term it appears in:

```yaml
variables:  {x: {foreach: [snapshot], bounds: {lower: 0, upper: 10}}}
constraints:
  cap:
    foreach: [snapshot, bus]
    equations:
      - expression: group_sum(x * w, over=generator, by=bus) <= limit   # w: [generator]
```

With `g1,g2 → b1` (w = 1.0, 2.0) and `g3 → b2` (w = 5.0):

| | `main` (duckdb) | this PR |
|---|---|---|
| matrix rows | 2 | **3** — `(0,0,2.0)` *and* `(0,0,1.0)` |
| LP text for `c0` | `+3.0 x0` | `+2.0 x0` / `+1.0 x0` |
| `solve()` | `6.0` ✅ | **crash** — `ShapeError: height of column 'value' (0) does not match height of column 'row' (2)` |
| same model, `integer: true` | `6.0` ✅ | **`status: ok`, `optimal`, objective `10.0`** ❌ |

The crash and the wrong answer have the same cause. `highs.py:108` calls `h.addRows(...)` and **never checks the return status**; HiGHS rejects a row list containing duplicate column indices:

```
addRows status: HighsStatus.kError
numRow after:   0
```

So *every constraint is dropped*. On an LP that surfaces as an incidental `ShapeError` when the dual read-back finds an empty `row_dual`; on a MIP there is no dual, nothing checks anything, and you get a confidently-reported optimal solution to an unconstrained problem. The LP-file sink emits duplicate terms that most parsers happen to sum, which is why the differential suite — including `transport` — misses this: `balance` has three term fragments, so `len(terms) != 1` forces the aggregate anyway.

**Fix** (`compiler.py:374`) — one line, verified:

```python
return TermFragment(
    (*keep, g.into), frame, p.is_term,
    p.keyed and g.over in p.label_dims,          # broadcast over ⇒ the key does not survive
    _relabel(p.label_dims, g.over, g.into),
)
```

Matrix becomes `(0,0,3.0) / (1,0,5.0)`, objective `6.0`, and the suite stays at `380 passed` — which also confirms **no existing test covers this shape**. The docstring on `_group_fragment` ("the join neither duplicates nor drops a term") is about *row count*, which is true; it's the *key* that doesn't survive, and the comment currently reads as if it justified the `keyed` pass-through.

**Independently worth fixing:** check the `HighsStatus` from `addCols`/`addRows` and raise. A sink that can silently load nothing is the reason this bug reports `optimal`. (Unchecked on `main` too — pre-existing, but this PR makes it load-bearing.)

**Process note:** `keyed`, `label_dims`, `survives_dropping` and `_needs_aggregate` are all *new here* — `main` has none of them. A correctness-critical aggregate-elision optimization is riding along inside an engine swap, where the differential suite is doing double duty as the only check on both. Worth splitting out, or at minimum adding the broadcast-`group_sum` case to `tests/test_group_sum.py`.

---

## 🟡 The `#109` determinism claim does not hold

> **LP output is deterministic** — every section written in label order, so the same model builds the same bytes twice (#109).

Three runs of the same model (4,000 generators / 200 buses / 400 lines / 40 snapshots, `examples/transport.yaml`) gave **three different SHA-256s**. Terms within a constraint are not in label order at all:

```
run 1  c0:  +1.0 x3800  +1.0 x1800  -1.0 x160000  +1.0 x2200  ...
run 2  c0:  +1.0 x1200  +1.0 x800   -1.0 x160200  +1.0 x160199 ...
```

`lp_file.py:130` sorts `(row, col)` in `_sorted_terms`, then `_constraint_blocks` does `.join(...).group_by('row').agg(str.join)` — the hash join discards the right side's order and the streaming `group_by` under `sink_csv` gives no within-group ordering guarantee. The sort is paid for and thrown away.

`main` is non-deterministic here too, so this is **not a regression** — but the claim is in the PR body and there is **no test for it** (`grep -rn "deterministic\|109" tests/` → nothing). Don't close #109 on this PR. The fix is to sort *after* the join (`.join(...).sort('row','col').group_by('row', maintain_order=True).agg(...)`), verified by a hash-equality test at a size large enough to actually split into morsels — a small model won't catch it.

---

## 🟠 The strategic question this PR reopens

The earlier recorded finding rejected exactly this swap, and the surviving arguments were the absolute memory gap on join+group-by and the rewrite cost — not the `memory_limit` knob. The PR's table lands on the same side at the top end (10M: 1.40× peak; transport 9.8M: **2.14×**), and it says so plainly, which is better than a buried regression.

Two things genuinely move the argument, and they're both in here:

- **End-to-end framing.** 7.40 GB vs 7.72 GB build+solve at 10M variables, because HiGHS dominates by ~8×. If nobody builds without solving, the write-path ratio is the wrong number to optimize. That's a real answer to the earlier objection, not a dodge.
- **Below ~1M variables you win on both axes by 2–3×**, which is where most models actually live.

What is worth stating before merging is which of these the project is choosing to be true, because ROADMAP Track 5 currently promises the memory axis back via partition-wise execution and that is now load-bearing rather than optional. The 2.14× on `transport` is the mixed-density case — the shape most likely to be someone's real model.

Separately: the two deletions are worth more than the benchmark table. `sql.py` and every quoting rule going away, and `from`/`order` becoming legal dimension names, are the kind of thing that stops generating bug reports forever. Both confirmed — `from` and `order` work here and fail on `main` with a raw duckdb parser error.

---

## Smaller items

- **Reserved-name gap.** With the identifier regex gone, a handful of names now collide with internal frame columns and leak polars internals: dimension `value` → `ValueError: cannot insert value, already exists`; dimension `row` → `DuplicateError` plus a dumped query plan. Both fail on `main` too (pre-existing), but the PR headlines name freedom, so a validation-time reserved-name check (`value`, `val`, `ord`, `row`, `col`, `var_label`, `coeff`, `cval`, `lb`, `ub`, `sense`, `rhs`, `vtype`) is the natural follow-up. The `'__ord {d}__'` / `'__rhs value__'` scratch columns rely on spaces being unrepresentable — which was true under the old regex and now isn't; worth a comment update either way.

- **`highs.py:98-102` rescans the whole matrix per chunk.** `ordered_matrix.filter(pl.col('row').is_between(lo, hi))` inside the chunk loop is O(chunks × nnz). Measured: 25M nnz / 50 chunks → **0.57 s** for the filter loop vs **0.00 s** for the equivalent `searchsorted` + `.slice()`. Both frames are already sorted and `row` is dense, so `ordered_rows.slice(lo, hi-lo)` is exact and the matrix boundaries are one `np.searchsorted` over the chunk edges. Modest, but it lands right where the 10M case went to 1.02×.

- **New validation, unannounced.** `_check_one_row_per_coordinate` (`executor.py:290`) has no equivalent on `main` — data with duplicate parameter coordinates that previously built now raises `DataError`. It's the right call and the message is excellent, but it's a user-visible break worth a line in the PR body / CHANGELOG, and it's also what `keyed` on a parameter fragment rests on.

- **Breaking API changes not called out.** `Result.primal()` now returns `polars.DataFrame` rather than pandas; `build()` drops `memory_limit` / `chunk_rows` / `threads` / `workdir` (callers passing them get `TypeError`); `to_pandas` / `to_dataarray` stay on `Result` but now need the `[linopy]` extra. All defensible for a `feat!`, but they belong in the body next to the engine swap, not only in docstrings. `api.py` still says "Build options stay separate, because they govern *construction*" — now vacuous, since there are none.

- **`np.nan_to_num` in `highs.py:88`** turns a NaN bound into `0.0` (its default `nan=0.0`), and polars' `is_null()` doesn't catch NaN, so the `_build_variable` null-bound guard won't either. Pre-existing on `main`; noting it since the bound guard reads as if it were exhaustive.

- **Rebase.** The body mentions `e544400` / `653ef88` riding along; the branch is now `2a195fa` and cleanly on top of `main`, so that paragraph is stale.

---

## Verdict

The rewrite itself is good work — the module split reads better than the SQL version did, `frames.py` is a genuinely nice boundary, and the docs were updated honestly including the parts that don't flatter the change.

**Not mergeable as-is** on the `_group_fragment` `keyed` bug: it produces a confidently-reported wrong answer with no error on a MIP. The one-line fix is verified above. Also wanted: the `HighsStatus` check, a test for the broadcast-`group_sum` shape, and either a determinism test or the `#109` claim dropped from the body.

The engine choice itself is a project decision, not a review finding — but the PR's numbers and the earlier measurement agree on where the cost lands, so it's worth deciding explicitly rather than by merge.
