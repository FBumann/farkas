---
marp: true
paginate: true
theme: default
style: |
  :root {
    --accent: #0d9488;
    --accent-ink: #0b7d72;
    --accent-bg: #e6f2f0;
    --warn: #a85c14;
    --ink: #131a1e;
    --muted: #5a6672;
    --faint: #9aa6ad;
    --hair: #dde3e6;
  }
  section {
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    color: var(--ink);
    font-size: 25px;
    padding: 54px 66px;
  }
  h1 { color: var(--ink); letter-spacing: -.02em; line-height: 1.08; margin: 0 0 .35em; }
  h2 { color: var(--accent-ink); font-size: 1.5em; letter-spacing: -.015em; margin: 0 0 .55em; }
  strong { color: var(--accent-ink); }
  em { color: var(--warn); font-style: normal; font-weight: 600; }
  code {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    background: #eef1f2; color: var(--ink); padding: .08em .32em; border-radius: 5px; font-size: .82em;
  }
  ul { line-height: 1.42; margin: 0; }
  li { margin-bottom: .32em; }
  .kicker {
    font-family: ui-monospace, Menlo, monospace; font-size: .58em; letter-spacing: .13em;
    text-transform: uppercase; color: var(--accent-ink); margin-bottom: 14px; display: block;
  }
  .lead { color: var(--muted); font-size: 1.05em; }
  .two { display: flex; gap: 44px; align-items: center; }
  .two > div { flex: 1; }
  footer { color: var(--faint); font-size: 13px; }

  /* --- architecture diagram --- */
  section.arch { font-size: 20px; padding: 40px 54px; }
  section.arch h2 { margin-bottom: .3em; }
  .arch { display: flex; flex-direction: column; gap: 6px; }
  .tlab { font-family: ui-monospace, Menlo, monospace; font-size: 12px; letter-spacing: .1em;
    text-transform: uppercase; color: var(--faint); display: block; margin-bottom: 6px; }
  .row { display: flex; gap: 16px; justify-content: center; }
  .box { border-radius: 10px; padding: 9px 14px; font-size: 15px; line-height: 1.25;
    background: #fff; border: 2px solid var(--accent); color: var(--accent-ink); font-weight: 600; text-align: center; }
  .box small { display: block; font-weight: 400; color: var(--muted); font-size: 12px; }
  .box.built { background: var(--accent-bg); }
  .box.road { border: 2px dashed var(--hair); color: var(--muted); background: #fff; font-weight: 500; }
  .box code { font-size: 13px; background: rgba(13,148,136,.09); }
  .flow { text-align: center; color: var(--faint); font-size: 18px; line-height: 1; margin: -1px 0; }
  .waist { background: var(--accent); color: #fff; border-radius: 12px; padding: 13px 18px;
    text-align: center; font-weight: 700; font-size: 20px; letter-spacing: -.01em; }
  .waist small { display: block; font-weight: 400; font-size: 13px; opacity: .92; margin-top: 3px; letter-spacing: 0; }
  .consumers { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-top: 4px; }
  .ccol { display: flex; flex-direction: column; gap: 8px; }
  .legend2 { display: flex; gap: 22px; justify-content: center; margin-top: 8px; font-size: 13px; color: var(--muted); }
  .legend2 span { display: inline-flex; align-items: center; gap: 7px; }
  .sw { width: 15px; height: 15px; border-radius: 4px; }
  .sw.b { background: var(--accent-bg); border: 2px solid var(--accent); }
  .sw.r { border: 2px dashed var(--hair); }
---

<!-- _paginate: false -->

<span class="kicker">linopy_yaml · SPEC §12 · issue #21</span>

# What is the problem?

Optimisation models get **built imperatively, welded to one solver, and
materialised dense**. linopy pads to NaN-filled `xarray` arrays at every
operator, so build peak memory is *O(dense dim product)* — a 35.6M-variable
dispatch model peaks at *6.6 GB*, and 3× bigger *won't fit in RAM* at all.

Worse, the *definition* of the model is tangled up with **how** it's built,
written, and solved. Want a memory-safe build? A different solver? A LaTeX
export? Today each means reaching back into imperative builder code. **One
model, many needs — but no shared, portable representation to hang them on.**

---

## What we built — the MVP

<div class="two">
<div>

One YAML spec → a **typed algebraic AST** → **two differential-tested
backends**, picked automatically:

- **Eager** — xarray → `linopy.Model`. The feature-complete default.
- **Relational** — the AST compiled to a **duckdb** query plan, streamed to any
  solver under a `memory_limit`. Masks → missing rows, `sum` → `GROUP BY`,
  labels → solver indices.

Same answers, proven against each other. Build peak memory becomes a **config
knob**, not the dense grid.

</div>
<div>

![w:440](bench.svg)

<span class="lead" style="font-size:14px">**13.6× lower peak RSS** · flat in model size · 107M vars stream at **0.57 GB**</span>

</div>
</div>

---

<!-- _class: arch -->

## The architecture: the AST is the narrow waist

<div class="arch">

  <span class="tlab" style="text-align:center">producers</span>
  <div class="row">
    <div class="box built">YAML front-end</div>
    <div class="box road">Python DSL <small>2nd producer</small></div>
  </div>
  <div class="flow">↓</div>

  <div class="waist">TYPED AST — one portable, solver-agnostic model
    <small>schema + expression / where nodes · the single contract every producer and consumer shares</small></div>
  <div class="flow">↓</div>

  <div class="consumers">
    <div class="ccol">
      <span class="tlab">backends · sinks (need data)</span>
      <div class="box built">eager → <code>linopy.Model</code></div>
      <div class="box built">relational → duckdb → lp · mps · HiGHS · Gurobi</div>
    </div>
    <div class="ccol">
      <span class="tlab">analyzers · AST only</span>
      <div class="box road">shape / dim inference</div>
      <div class="box road">size estimator → router</div>
    </div>
    <div class="ccol">
      <span class="tlab">renderers · AST only</span>
      <div class="box road">LaTeX / MathML</div>
      <div class="box road">AMPL / GAMS / Pyomo</div>
    </div>
    <div class="ccol">
      <span class="tlab">transformers · AST→AST</span>
      <div class="box road">piecewise / SOS → aux vars</div>
      <div class="box road">presolve / const-fold</div>
    </div>
  </div>

  <div class="legend2">
    <span><i class="sw b"></i> built today (MVP)</span>
    <span><i class="sw r"></i> unlocked by the waist — a consumer, not a rewrite</span>
  </div>
</div>

<!-- _footer: The two backends are just the first two consumers. Everything downstream reads the same AST. — issue #21 -->
