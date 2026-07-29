---
hide:
  - navigation
  - toc
---

<div class="hero" markdown>

# farkas

**Self-documenting optimisation models — at any scale.**

Write the math in YAML, bind data at runtime, solve.

[![PyPI](https://img.shields.io/pypi/v/farkas)](https://pypi.org/project/farkas/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

[Write a model](guide.md){ .md-button .md-button--primary }
[Browse the models](models/index.md){ .md-button }

</div>

---

<div class="landing" markdown>

<div class="grid cards" markdown>

-   :material-file-document-outline: __Declarative math__

    ---

    Readable without knowing the implementation, and self-contained: no Python
    state changes what a file means. It diffs cleanly in review and travels as a
    research artefact.

-   :material-grid: __Sparse by construction__

    ---

    A mask is an absent row, never a NaN in a dense array — a model pays for the
    variables it has, not for its coordinate product. Labels *are* the solver's
    own row and column indices.

-   :material-alert-octagon-outline: __Fail early, fail loud__

    ---

    Every expression, `where` string and even *uncalled* macro template is
    parsed and name-checked before a single source is bound. Errors name the
    problem and its rewrite.

-   :material-fence: __A finite language, with a priced way out__

    ---

    The ceiling is a closure (affine ∩ relational ∩ local), not a feature race.
    Genuinely unsayable math goes in an `escape:` island — visible in the file,
    billed before it runs.

-   :material-speedometer: __Straight to the solver__

    ---

    YAML and data in, a populated solver out, no LP file in between: 2–4x faster
    than the eager lane on four of five benchmark cases, lower peak memory on
    all five. [The numbers](benchmarks.md)

-   :material-check-decagram-outline: __Checked against somebody else__

    ---

    Eleven models in the gallery match an optimum this project did not compute —
    GAMS, PyPSA, OR-Library, TSPLIB — objectives *and* shadow prices.
    [The corpus](ports.md)

</div>

{%
   include-markdown "../README.md"
   start="<!--flow-start-->"
   end="<!--flow-end-->"
%}

## The whole thing, in one model

{%
   include-markdown "../README.md"
   start="<!--quickstart-start-->"
   end="<!--quickstart-end-->"
%}

## The same file, as math

A declared model can be *printed* — the way a paper prints it, from the file
above and nothing else. Which makes it the cheapest review tool here: read the
math, not the YAML, and see whether it says what you meant.

<!-- home-math:begin -->
=== "The math"

    #### Objective

    **`total_cost`**

    $$\min \sum_{s \in \mathcal{S},\enspace g \in \mathcal{G}} p_{s,g} \cdot c_{g}$$

    #### Subject to

    **`power_balance`**

    $$\sum_{g \in \mathcal{G}} p_{s,g} = \ell_{s} \qquad \forall\thinspace s \in \mathcal{S}$$

    #### Variable domains

    **`p`**

    $$0 \le p_{s,g} \le \bar p_{g} \qquad \forall\thinspace s \in \mathcal{S},\enspace g \in \mathcal{G} \thinspace:\thinspace \bar p_{g} > 0$$

=== "LaTeX"

    ```latex
    \paragraph{Objective}
    \begin{align}
    \text{total\_cost} && \min & \sum_{s \in \mathcal{S},\ g \in \mathcal{G}} p_{s,g} \cdot c_{g}
    \end{align}

    \paragraph{Subject to}
    \begin{align}
    \text{power\_balance} && \sum_{g \in \mathcal{G}} p_{s,g} & = \ell_{s} && \forall\, s \in \mathcal{S}
    \end{align}

    \paragraph{Variable domains}
    \begin{align}
    \text{p} && 0 \le p_{s,g} & \le \bar p_{g} && \forall\, s \in \mathcal{S},\ g \in \mathcal{G} \,:\, \bar p_{g} > 0
    \end{align}
    ```

=== "How"

    ```python
    import farkas as fk

    fk.to_latex('dispatch.yaml')  # amsmath align
    fk.to_typst('dispatch.yaml')  # compiles without a TeX toolchain
    fk.to_markdown('dispatch.yaml')  # renders as-is on GitHub
    ```

    No data, no solver, no lane: it reads the same validated model both lanes read,
    so a `piecewise:` block prints as the formulation it expands to rather than the
    sugar it was written as. `--symbols` points at a sidecar table when the derived
    symbols are not the ones your paper uses, and `--standalone` emits a document
    that compiles.

    ```bash
    python -m farkas latex dispatch.yaml --standalone -o dispatch.tex
    ```
<!-- home-math:end -->

## Where to next

<div class="grid cards" markdown>

-   :material-school: __Writing a model__

    ---

    Five ideas — dimensions, absence, topology, `roll`, the dim algebra — each
    shown in a model that runs, and what the language will *not* do.

    [:octicons-arrow-right-24: The guide](guide.md)

-   :material-view-gallery-outline: __Models__

    ---

    Every model in the repo, what each exercises, and which ones are checked
    against an optimum from elsewhere.

    [:octicons-arrow-right-24: The gallery](models/index.md)

-   :material-book-open-page-variant: __Language reference__

    ---

    What a YAML file may contain, and what it means.

    [:octicons-arrow-right-24: SPEC](SPEC.md)

-   :material-source-branch: __Why it is shaped this way__

    ---

    The hard rules, the expressive ceiling, the module map — and what we have
    decided never to build.

    [:octicons-arrow-right-24: Architecture](ARCHITECTURE.md) ·
    [Roadmap](ROADMAP.md)

</div>

```bash
pip install farkas  # the relational engine (polars, highspy)
pip install "farkas[linopy]"  # adds linopy + xarray + pandas: the shim, the
                              # oracle, and to_pandas / to_dataarray
```

!!! warning "Alpha, pre-1.0"

    **Breaking changes land without a deprecation cycle.** When a construct is
    named wrong, a default is wrong, or a permissive input turns out to hide a
    silent wrong answer, it gets fixed rather than aliased. Pin an exact version
    if you depend on this, and read the [changelog](changelog.md) before
    upgrading — breaking commits are marked `!`, and every one names the
    rewrite. It is the *surface* that is not yet frozen, not the behaviour.

</div>
