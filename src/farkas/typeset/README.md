# `typeset/` — the model, printed

A third consumer of the resolved core AST. It builds no model, binds no data
and never reaches the plan; it walks the same typed tree both lanes consume and
prints it.

| Module | Role |
|---|---|
| `__init__.py` | `typeset` / `to_latex` / `to_typst`, and the `FORMATS` registry |
| `walk.py` | resolved AST → `Line`s. Every decision about the **math**, written once |
| `format.py` | the seam: what a format must spell, and the operator vocabulary |
| `symbols.py` | which symbol a name gets, and the `SymbolTable` sidecar that overrides it |
| `latex.py` | amsmath — the format that lands in a journal |
| `typst.py` | Typst — the format that compiles without a toolchain |

## The split, and why it is here

`walk.py` decides where a bracket changes the reading, which dimension a
reduction binds, that a mask belongs on the ∀ rather than in the equation, and
that a translation shows at the leaf it re-indexes. A `Format` decides that a
sum is `\sum_{…}` or `sum_(…)`.

Those are different questions, and the reason they are in different files is
the same reason `relational/sinks/` exists: with one module the second format
becomes a *copy of the walk*, and two copies of a walk are two walks that can
disagree about what the model says. This is the same divergence hard rule 3
spends its budget preventing at the other end of the pipeline — and it matters
more here than it looks, because a typeset model is what a reader checks the
math against.

Two rules keep it honest:

- **A walk emits bare math** — no `$`, no environment. The format wraps it with
  `math()` when embedding in prose, so the walk never knows which mode it is in.
- **A format spells; it never decides.** No method in `format.py` takes an AST
  node or a schema. If a format had to look at the model to answer, the
  question belongs in the walk.

## Adding a format

1. A module here with a class satisfying `Format` — atoms, structure, document,
   and a spelling for every name in `OPERATOR_NAMES`.
2. A row in `FORMATS` in `__init__.py`. The CLI verb comes from the key.
3. Nothing in `walk.py`. If you need to change it, either the walk is making a
   syntax decision it should not, or the seam is missing a method — fix that
   rather than special-casing.

`tests/test_typeset.py` runs the shared expectations against **every** entry in
`FORMATS`, so a new format inherits the suite. Two of them are the point: every
operator name is spelled, and no format leaks another's syntax.

## Verified, not assumed

Both formats are **compiled** in CI, not just string-matched. LaTeX needs a
two-package apt install; Typst is a pip wheel, so the suite compiles it
in-process. Structural checks (brace balance, environment nesting,
`\left`/`\right` pairing) run too — they are what a *generator* gets wrong —
but they are not a compile, and a malformed `\mathcal` passes every one of them.
