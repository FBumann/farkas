# Glossary

The vocabulary of the language, one entry per construct, with the name it
carries at every layer it passes through. Companion to
[ARCHITECTURE.md](ARCHITECTURE.md) (why the set is closed and what a new
member costs), [SPEC.md](SPEC.md) (exact syntax and semantics), and
[ROADMAP.md](ROADMAP.md) (what is planned and what is refused).

If a PR adds, renames, or retires a construct, it updates this file.

## 0. Kinds — what "primitive" actually covers

`ARCHITECTURE.md` writes the tier-1 set as "operators · sum · group_sum ·
roll/shift · where · piecewise:". Those are five different kinds of thing
sharing one word. They differ in what they cost and in where they are
consumed, so the glossary separates them:

| Kind | What it is | Consumed by | Taxed? |
|---|---|---|---|
| **Declaration form** | a top-level YAML key that introduces model objects | schema + both lanes | structural |
| **Arithmetic operator** | combines expressions without changing their index set | both lanes (IR node) | yes |
| **Shape operator** | re-indexes one dimension of an expression | both lanes (IR node) | yes |
| **Predicate** | a `where` term; decides row presence | both lanes (IR node) | yes |
| **Formulation** | expands to *new declarations* before dispatch | neither lane sees it | yes (Python) |
| **Abstraction form** | expands to *the same AST* before dispatch | neither lane sees it | no |
| **Escape** | declared, terminal, budgeted foreign math (#38) | sink only | budgeted |

**Primitive** = arithmetic operator ∪ shape operator ∪ predicate. Those are
exactly the constructs with an IR node and a lowering case — the things that
carry the two-backend tax described in ARCHITECTURE.

A **formulation** is taxed like a primitive (it is Python, it needs
differential tests) but composes like a macro (it is gone before either
backend runs). `piecewise:` is the only one. It is not a primitive: SPEC
§12.4 is explicit that formulations never enter as IR expression nodes.

An **abstraction form** (`macros:`, `expressions:`) is free — pure AST
substitution, no backend ever sees it, no divergence risk.

---

## 1. Declaration forms

The nouns. Each introduces named objects into the model.

| Key | Introduces | Notes |
|---|---|---|
| `dimensions:` | an index set | `dtype ∈ {float, int, str, datetime}`; `values:` optional (else inferred from data) |
| `parameters:` | known data over dims | shape only in YAML; data binds at run time by name |
| `variables:` | decision variables | `foreach`, `where`, `bounds`, `binary`/`integer` |
| `constraints:` | affine relations | `foreach`, `where`, `equations[]` |
| `objectives:` | the optimisation sense + expression | last one wins; only `equations[0]` is used |
| `expressions:` | named 0-ary expression | abstraction form (§5) |
| `macros:` | parameterised expression template | abstraction form (§5) |
| `piecewise:` | a λ-formulation | formulation (§6) |

Modifiers that appear inside them:

**`foreach`** — the index set a declaration is instantiated over. Reads as
iteration; it is not. It is the dim signature of the declared object, known
before any data is touched. (Parameters spell the same idea `dims:` — see
§8, open naming item 5.)

**`where`** — a predicate restricting which coordinate combinations exist.
Semantics are **row absence**, not zero-fill: a masked-out variable is not
created, a masked-out constraint row is not built. Constraint-level and
equation-level `where` strings are ANDed.

**`bounds`** — `lower` / `upper`, each a number or a parameter name.
Expressions here are #31, not current.

**`binary` / `integer`** — variable *type*, not a formulation: they become a
`vtype` column and native solver integrality, which is what keeps basic MILP
inside the streaming lane.

**`equations`** — the list of `expression` (+ optional `where`) pairs under a
constraint or objective. A constraint expression carries exactly one
comparison; an objective expression carries none.

---

## 2. Arithmetic operators

`+`  `-`  `*`  `/`  `**`, plus unary `+` / `-`.

They combine expressions **without re-indexing**: the dim signature of the
result is the union of its operands' signatures. Everything that changes a
dim signature is in §3.

Two rules make them affine:

- `*` — at least one factor must be variable-free. Two variable-carrying
  factors is a load error (`nonlinear product`).
- `/` — the divisor must be variable-free *and* a single factor, not a sum.

**`**` is outside the language, on both lanes.** It parses (SPEC §5.1) — so
that the refusal can name the operator and its rewrite rather than dying in
the grammar — and is then refused at load time: `lowering.py` has no case
for it, and `builder.py:246` raises rather than evaluating it. Membership is
what settled the direction: with a variable base it breaks degree 1, and
over parameters alone it is data prep (SPEC §10).

---

## 3. Shape operators

The taxed core. **A shape operator re-indexes exactly one dimension of an
expression.** That single sentence covers the whole set, current and
planned, and it is what the executor literally does — `_sum_piece`,
`_group_piece` and `_shift_piece` each emit a `SELECT` that rewrites one dim
column of the term stream and nothing else.

Note what is *not* in any of them: the summation. Terms landing on the same
output coordinate are added by the final coefficient assembly, because an LP
row is a sum of terms. That is true of `+` too. So the `sum` in `sum` and
`group_sum` names a consequence, not the operation — the operation is the
coordinate map (§8, open naming item 2).

Two families, and the family decides both the SQL and the locality class:

| Family | Coordinate map | Relational | Locality |
|---|---|---|---|
| **Pushforward** (fan-in) | source coord → output coord | JOIN, then the universal aggregate | pointwise, if fan-in is bounded |
| **Pullback** (fan-out) | output coord → source coord | JOIN, no aggregate | bounded-halo or pointwise |

ARCHITECTURE already states this without naming it — the planned lookup
operator is described as "the `group_sum` join without the `GROUP BY`".
Pullback and pushforward along the same mapping parameter are adjoint; they
are the same table read in opposite directions.

### Current

**`sum(array, over=dim)`** — pushforward along the constant map: `dim`
collapses. `dim` must name a declared dimension, or a macro formal bound to
one; that is checked at load time on both lanes
(`resolution.py:_DIM_VALUE_KWARGS`). If the *operand* does not carry `dim`
the call is a deliberate no-op (`lowering.py:269`), for parity with the
eager lane. One dim per call at the surface; `ir.Sum.over` is a tuple, but
lowering only ever fills it with one.

**`group_sum(array, mapping, into=dim)`** — pushforward along a mapping
parameter. `mapping` is a 1-D parameter whose *values* are group labels; its
dim is replaced by `into`. This is the membership sum ("all generators at
this bus"), and it is what makes topology data rather than structure. The
source dim is implicit — it is read off the mapping's declaration, not
written at the call site. So is the *target*: the mapping declares
`values_in: bus` (SPEC §3.2), and `into=bus` at the call site has to agree.
That declaration is what types the value column as index data, so a label
that is no coordinate is refused when the data is bound instead of quietly
joining to nothing.

**`roll(array, dim=n)`** — pullback along a cyclic translation. The result
at coordinate *t* is the input at *t−n* (so `roll(soc, snapshot=1)` is "soc
one step ago"). Coordinates stay fixed and values wrap: `roll_coords=False`
eagerly, modulo arithmetic on the dim table's `ord` column relationally.

**`shift(array, dim=n)`** — the acyclic counterpart. Identical, minus the
modulo: positions shifted past the edge contribute zero — by `fill_value=0`
eagerly, by non-joining rows relationally. Use for recurrences with a real
start (storage that begins empty).

`roll` and `shift` are **one IR node**, `Shift(x, dim, n, wrap)`. They are
also the language's own counterexample on macro-friendliness: the dimension
sits in a kwarg *key*, so no macro can parameterise it (§8, open naming
item 1).

### Planned (ROADMAP Track 1)

| Surface | Family | Re-indexes | Item |
|---|---|---|---|
| `at(x, over=d, index=value)` | pullback (constant) | `d` collapses by selection | 1 |
| `at(x, over=d, index=map)` | pullback (gather) | `d` → the map's declared `values_in` | 2 |
| `sum_next_n(x, over=d, n=N)` | pushforward (window) | `d` → `d`, bounded halo | 4 |
| `where(x, cond)` | filter | nothing; drops terms | 10 |

`at` with a lookup is the adjoint of `group_sum`: same mapping table, join
without the aggregate.

---

## 4. Predicates

The `where` sub-language. Its result is always a boolean mask; its meaning
is always row absence.

A bare name and a comparison are each typed by `resolution.py` into one of
two predicates, according to what the name turns out to denote:

| Surface | Names a… | Meaning |
|---|---|---|
| `name` (bare) | parameter | **defined**: non-null and finite (`ParamDefined`) |
| `name` (bare) | dimension | true over its whole coordinate index (`DimDefined`) |
| `name OP value` | parameter | element-wise comparison (`ParamCmp`); `OP ∈ {==, !=, <, >, <=, >=}`; NaN → False |
| `name OP value` | dimension | comparison against the frame's own coordinate column (`DimCmp`) |
| `AND` / `OR` / `NOT` | — | boolean logic, case-insensitive; `NOT` > `AND` > `OR` |
| `True` / `False` | — | literals; `True` ≡ no `where` at all |

**A bare name means different things in the two grammars.** In an
expression it is the value; in a `where` it is "is defined". This is
deliberate and it is also the language's sharpest context-sensitivity (§8,
open naming item 4).

Unknown names in a `where` are a **load error**. They used to evaluate to
scalar False, which silently produced an empty model; resolution now types
every bare name as a parameter or a dimension and fails on anything else, so
both grammars are name-checked.

**Comparisons against a dimension coordinate** (`where: "snapshot > 0"`) are
in the language on both lanes. They need no join — the frame already carries
its own coordinates, so the predicate is a filter on a column that is
already there. A dimension *outside* the declaration's `foreach` is still
rejected: the mask would have to be `any`-reduced over a dim the declaration
never named.

A mask carrying a dim not in `foreach` is a **load error** (`dimensions.py`,
SPEC §6.3). It used to be reduced with `any` before broadcasting, which fails
*open*: one true value anywhere along the reduced dim included every
coordinate. Neither reduction has a spelling today — `any` and `all` are both
ROADMAP Track 1 item 6.

---

## 5. Abstraction forms — free composition

**Named expression** (`expressions:`) — a name bound to an expression
string, substituted at every use site before dispatch.

**Macro** (`macros:`) — a parameterised expression template: `args`
(positional) and `kwargs` (keyword) formals shadow model names inside the
template; call sites expand to core AST. Macros are *language, not code* —
schema-local, validated at load time even when never called, and invisible
to both backends. This is the intended home for every composition of
primitives.

A named expression is a macro with no formals. They are two blocks for one
concept, which the schema then has to police for cross-collisions (§8, open
naming item 6).

---

## 6. Formulations

**`piecewise:`** — N expressions jointly pinned to a breakpoint-indexed
curve, expanded at schema level into ordinary variables and constraints (λ
convex-combination; binaries via `vtype`, adjacency via `shift`). Options:
`convex: true` for a pure-LP hull with no binaries, `active:` for gating.

The defining property of a formulation, and why it gets its own kind: it
**emits declarations**, where a macro emits only an expression. Both are
gone before dispatch, so both backends receive identical affine
declarations and stay differential-testable. SOS and indicator constraints
(#23) would be formulations if they ever land — never expression nodes, and
only once the sinks carry the corresponding streams.

---

## 7. Cross-layer name map

The same construct wears up to five names. This table is the index; drift
between columns is a bug.

| YAML surface | Expression/where AST | IR node | Relational | Eager |
|---|---|---|---|---|
| number literal | `NumberNode` | `Const` | literal | scalar |
| parameter name | `NameNode` | `Param` | `p_<name>` table | `xr.DataArray` |
| variable name | `NameNode` | `Var` | `var_label` column | `linopy.Variable` |
| `-x` | `UnaryOpNode('-')` | `Neg` | negated coeff | `-x` |
| `a + b`, `a - b` | `BinOpNode` | `Add` (+ `Neg`) | term concat | `+` / `-` |
| `a * b` | `BinOpNode('*')` | `Mul` | join, coeff product | `*` |
| `a / b` | `BinOpNode('/')` | `Div` | join, coeff quotient | `/` |
| `a ** b` | `BinOpNode('**')` | **none** | refused at load | refused at load |
| `sum(x, over=d)` | `FuncCallNode` | `Sum` | drop dim column | `.sum(d)` |
| `group_sum(x, m, into=b)` | `FuncCallNode` | `GroupSum` | join mapping, relabel | `.groupby(m).sum()` |
| `roll(x, d=n)` | `FuncCallNode` | `Shift(wrap=True)` | ord join, modulo | `.roll(roll_coords=False)` |
| `shift(x, d=n)` | `FuncCallNode` | `Shift(wrap=False)` | ord join, no modulo | `.shift(fill_value=0)` |
| `<=` `>=` `==` | `CompareNode` | `ConstraintDecl.sense` | row sense | linopy `sign` |
| bare parameter in `where` | `ExistenceCheck` → `ParamDefined` | `Defined` | `IS NOT NULL AND isfinite` | `notnull() & isfinite` |
| bare dimension in `where` | `ExistenceCheck` → `DimDefined` | `Bool(True)` | no filter | all-True mask |
| `param OP value` | `Comparison` → `ParamCmp` | `Cmp` | `LEFT JOIN` + `WHERE` | element-wise compare |
| `dim OP value` | `Comparison` → `DimCmp` | `DimCmp` | `WHERE` on the frame's own column | coordinate compare |
| `True` / `False` | `BoolLiteral` | `Bool` | constant filter | scalar mask |
| `AND` / `OR` / `NOT` | `AndNode` / `OrNode` / `NotNode` | `And` / `Or` / `Not` | `AND` / `OR` / `NOT` | `&` / `\|` / `~` |
| `foreach:` | — | `.dims` on the decl | grid table | master coords |
| `bounds:` | — | `.lower` / `.upper` | `lower`/`upper` columns | linopy bounds |
| `binary:` / `integer:` | — | `.vtype` | LP section, HiGHS integrality | linopy kwargs |
| `sense: minimize` | — | `ObjectiveDecl('min')` | LP objective | linopy sense |

The arrow in the where rows is not drift — it is the resolution pass.
`ExistenceCheck` and `Comparison` are *syntactic*: the grammar sees a name
and cannot know what it denotes. `resolution.py` types them, and everything
downstream consumes only the typed forms, which is why `_lower_where_node`
treats an unresolved node as an assertion failure rather than a case to
handle. Where the words still differ for one concept (`ParamDefined` vs
`Defined`, `ParamCmp` vs `Cmp`), the layer is the reason: the where-AST
names what the *name* denotes, the IR names what the *predicate* does.

---

## 8. Naming — conventions and open decisions

### Conventions in force

1. **Dimensions go in value positions, never kwarg keys.** `over=d`,
   `into=d` — so a macro can parameterise them. `roll`/`shift` violate this
   and are the stated counterexample.
2. **Surface names describe the math; internal names describe the layer.**
   Parser nodes are syntax, IR nodes are semantics, executor helpers are
   SQL. The three vocabularies may differ in *form* but must not differ in
   *concept*.
3. **Engine-internal naming encodes neither "duckdb" nor "yaml"** (hard rule
   2).
4. **A rejection names the construct and its rewrite** — never the other
   lane.

### Open decisions

**1. `roll` / `shift` → one operator with the dim in a value position.**
They are already one IR node. The surface split buys nothing and costs the
macro-friendliness rule the project wrote down for itself. Proposal:

```yaml
shift(soc, over=snapshot, by=1)              # acyclic (today's shift)
shift(soc, over=snapshot, by=1, wrap=true)   # cyclic  (today's roll)
```

If the pair survives, `lag` is the better name for the acyclic case: every
convention in reach (`pandas.shift(1)`, `xarray.roll(1)`, `numpy.roll(1)`)
means "the value one step ago", and `lag` says that where `shift(+1)` reads
to many people as "move forward".

**2. `group_sum` → `sum` with a `by=`.** The two are the same operator with
different coordinate maps, and today they disagree on argument shape:
`sum` puts its dim in a kwarg, `group_sum` takes its mapping positionally
and leaves its *source* dim entirely implicit. Proposal:

```yaml
sum(p, over=generator)                              # collapse
sum(p, over=generator, by=gen_bus, into=bus)        # collapse into groups
```

This makes the family visible, makes the source dim explicit at the call
site, and makes plain `sum` the degenerate case. The word "group" also
mis-cues: SQL's `GROUP BY` keys off a column of the same table, whereas here
the key arrives from a separate mapping parameter. If the operators stay
separate, `sum_by` beats `group_sum`.

**3. Dim names in operators — settled.** Every dimension-valued kwarg is now
checked against `dimensions:` at load time, on both lanes:
`resolution.py:_DIM_VALUE_KWARGS` covers `sum(over=)` and `group_sum(into=)`,
`_DIM_KEY_HELPERS` covers the `roll`/`shift` dim-as-key. A macro formal bound
to a dimension at the call site passes. What remains is not a name check:
`sum` over a dim the *operand* does not carry is still a silent no-op (§3).

**4. `defined` should be sayable.** The internal names are settled —
`ParamDefined`/`DimDefined` in the where-AST, `Defined` in the IR — but there
is still no surface spelling; a bare name is the only way to say it. ROADMAP
item 5 wants `defined(v)` over variables anyway: make it the explicit form
and keep the bare name as sugar.

**5. `foreach:` vs `dims:`.** One concept, two keys, split by which
declaration you are writing. Unifying is breaking; recording it here so the
next person does not re-derive the question.

**6. `expressions:` is `macros:` with no formals.** Two blocks for one
concept, plus a collision check between them in `schema.py`. Allowing
`macros:` entries to omit `args`/`kwargs` covers the case; `expressions:`
then becomes sugar or goes.

**7. "Helper" undersells them.** `helpers.py` says "built-in helper
functions", SPEC §7 agrees, ARCHITECTURE calls the same things primitives.
They are the taxed core of the language, not conveniences. Proposal: **shape
operator** in user-facing docs, `helpers.py` keeps its name as the eager
*evaluation* module.

---

## 9. Cross-language map

§7 maps a construct through *our* layers. This maps it outward, to the two
languages a reader most often arrives from: linopy (the object model we
build into) and Calliope (the declarative math language ours most resembles).

It is a **name index, not a scorecard**. Whether a construct of theirs is
one we should have is a scoping question, and it is answered in
[ROADMAP.md](ROADMAP.md#how-we-measure-capability), not here.

### Declaration forms

| Concept | linopy_yaml | linopy | Calliope |
|---|---|---|---|
| index set | `dimensions:` | `coords=` at `add_variables` | none in math — dims come from the model definition |
| known data | `parameters:` (shape only; bound at run time) | an `xr.DataArray` you pass in | model-definition params / lookup arrays |
| decision variable | `variables:` | `m.add_variables(lower, upper, coords, mask, binary, integer)` | `variables:` (`domain: real\|integer`, `bounds: {min, max}`) |
| affine relation | `constraints:` | `m.add_constraints(lhs, sign, rhs, mask)` | `constraints:` |
| objective | `objectives:` (`sense: minimize`) | `m.add_objective(expr, sense='min')` | `objectives:` (`sense: minimise`) |
| substituted expression | `expressions:` | a Python name holding a `LinearExpression` | `sub_expressions` (`$name`) |
| parameterised template | `macros:` | a Python function | — (their sub-expressions take no formals) |
| piecewise | `piecewise:` (λ) | `add_piecewise_formulation` | `piecewise_constraints:` (SOS2) |
| index signature | `foreach:` / `dims:` | `coords` | `foreach:` |
| row-absence mask | `where:` | `mask=` | `where:` |

### Operators

| linopy_yaml | linopy | Calliope |
|---|---|---|
| `sum(x, over=d)` | `.sum(dim=d)` | `sum(x, over=d)` |
| `group_sum(x, m, into=b)` | `.groupby(m).sum()` | `group_sum(x, m, b)` |
| `roll(x, d=n)` | `.roll(d=n, roll_coords=False)` | `roll(x, d=n)` |
| `shift(x, d=n)` | `.shift(d=n, fill_value=0)` | — |
| `a ** b` — refused | `**` | supported |
| *planned* `at` (ROADMAP 1–2) | `.sel()` / `.isel()` | `x[d=$slice]`, `select_from_lookup_arrays`, `get_val_at_index` |
| *planned* `sum_next_n` (ROADMAP 4) | `.rolling(...).sum()` | `sum_next_n` |
| — | — | `map_dim`, `group_datetime`, expression-level `where(x, cond)` |

### False friends

Four places where the same word means different things. These are the
reason this section exists.

1. **`active:`** — Calliope: a boolean that switches a component off.
   Ours: a *gating expression* on `piecewise:`, pinning the formulation to
   zero. Unrelated.
2. **`where`** — ours is only a declaration-level predicate. Calliope also
   has an expression-level `where(component, condition)` helper, which is a
   different construct wearing the same word.
3. **`expressions:`** — ours is Calliope's `sub_expressions` (substitution),
   **not** their `global_expressions`, which carry `foreach`/`where`/`order`
   and are materialised components. Naming ours after theirs mis-cues.
4. **`group_sum`** — near-identical spelling to theirs, but the word
   mis-describes both: SQL's `GROUP BY` keys off a column of the same table,
   while here the key arrives from a separate mapping parameter (§8, open
   decision 2).

And one structural difference worth stating, because it reads as a gap and
is not: Calliope's `order:` exists because their `global_expressions` are
materialised components that can depend on each other. Ours are
substitution, expanded before dispatch, so there is no evaluation order to
declare. The concept has nowhere to attach.
