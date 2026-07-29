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
   start="<!--model-start-->"
   end="<!--model-end-->"
%}

### And that file says, exactly this

Generated from the YAML above — no data, no solver, no second source of truth.
Only the notation is a choice, and **How** shows the one that was made here.

<!-- home-math:begin -->
=== "The math"

    #### Sets

    | Symbol | Meaning |
    |---|---|
    | $\mathcal{S}$ | index $s$ --- `snapshot` --- dispatch periods |
    | $\mathcal{G}$ | index $g$ --- `generator` --- generating units |

    #### Parameters

    | Symbol | Meaning |
    |---|---|
    | $\bar p$ | `p_max` over $\mathcal{G}$ --- installed capacity |
    | $\ell$ | `load` over $\mathcal{S}$ --- demand to be met |
    | $c$ | `cost` over $\mathcal{G}$ --- marginal cost |

    #### Variables

    | Symbol | Meaning |
    |---|---|
    | $p$ | `p` over $\mathcal{S} \times \mathcal{G}$ --- output of generator $g$ in snapshot $s$ |

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
    \paragraph{Sets}
    \begin{description}
    \item[$\mathcal{S}$] index $s$ --- \texttt{snapshot} --- dispatch periods
    \item[$\mathcal{G}$] index $g$ --- \texttt{generator} --- generating units
    \end{description}

    \paragraph{Parameters}
    \begin{description}
    \item[$\bar p$] \texttt{p\_max} over $\mathcal{G}$ --- installed capacity
    \item[$\ell$] \texttt{load} over $\mathcal{S}$ --- demand to be met
    \item[$c$] \texttt{cost} over $\mathcal{G}$ --- marginal cost
    \end{description}

    \paragraph{Variables}
    \begin{description}
    \item[$p$] \texttt{p} over $\mathcal{S} \times \mathcal{G}$ --- output of generator $g$ in snapshot $s$
    \end{description}

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

    symbols = {
        'dimensions': {
            'snapshot': {'index': 's', 'set': '\\mathcal{S}'},
            'generator': {'index': 'g', 'set': '\\mathcal{G}'},
        },
        'names': {
            'cost': 'c',
            'load': '\\ell',
            'p_max': '\\bar p',
        },
        'descriptions': {
            'snapshot': 'dispatch periods',
            'generator': 'generating units',
            'p': 'output of generator $g$ in snapshot $s$',
            'cost': 'marginal cost',
            'load': 'demand to be met',
            'p_max': 'installed capacity',
        },
    }

    fk.to_latex('dispatch.yaml', symbols=symbols)  # amsmath align
    fk.to_typst('dispatch.yaml')  # compiles without a TeX toolchain
    fk.to_markdown('dispatch.yaml')  # renders as-is on GitHub
    ```

    `symbols` is optional — drop it and the same model prints as
    $\mathit{load}_t$, $p^{\mathrm{max}}_g$. A dict, a YAML path or a
    `SymbolTable`; a key naming nothing in the model is an error, not a symbol that
    silently never applies.

    Or from a shell, where the table is that same YAML on disk and `--standalone`
    emits a document that compiles rather than a fragment to `\input`:

    ```bash
    python -m farkas latex dispatch.yaml --symbols dispatch.symbols.yaml
    python -m farkas typst dispatch.yaml --standalone -o dispatch.typ
    ```
<!-- home-math:end -->

### Then you solve it

{%
   include-markdown "../README.md"
   start="<!--solve-start-->"
   end="<!--quickstart-end-->"
%}

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
