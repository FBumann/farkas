# Language Reference

What a YAML file may contain and what it means. *Why* it is shaped this way:
[docs/ARCHITECTURE.md](ARCHITECTURE.md). What is planned or refused:
[docs/ROADMAP.md](ROADMAP.md). A worked example: [README](https://github.com/FBumann/lpspec/blob/main/README.md#example).

## 0. The laws

Ten rules the whole language reduces to. Every section below elaborates one,
and each law names the section that does — so a rule stated here is not
restated there.

**Nothing is guessed.** Where a file does not determine the answer, loading
fails and the message names the rewrite. Every law is that one principle,
applied in a different position.

| # | Law | § |
|---|---|---|
| 1 | Eight top-level keys, and the schema is **closed at every level** — an unknown key is an error naming the near miss. Booleans are YAML 1.2, so `no` / `on` / `off` stay labels. | [§1](#1-file-shape) |
| 2 | Everything decidable without data is **decided without data**. | [§9](#9-errors) |
| 3 | **One flat namespace, no shadowing** — a collision is a load error naming both declarations. | [§5.1](#51-name-resolution) |
| 4 | **Position decides which kinds of name are legal**, and a name's kind is fixed at load time. A dimension is never legal in a value position: it is a coordinate space, not data. | [§5.1](#51-name-resolution) |
| 5 | **Dim sets compose by union.** A constraint must *equal* its `foreach`; a `where` or a bound must not *exceed* its frame. | [§5.2](#52-dim-algebra) |
| 6 | **Absence is a property of variables.** Four constructs create it; nothing else does. | [§6](#6-absence) |
| 7 | Through arithmetic absence **spreads, taking the row with it**. Out of a reduction it does not — so a reduction does not distribute over `+`, and `sum(x + y)` and `sum(x) + sum(y)` are different questions. | [§6](#6-absence) |
| 8 | **Identity of the position.** A missing value reads as whatever makes it contribute nothing — zero as a coefficient, the identity of a sum; false in a `where`, where the coordinate then does not exist. Where no such reading exists it is refused: a divisor, a bound. `shift(…, edge=)` is the one place a value may be *asked* for, and it takes the identity of its position too. | [§6](#6-absence), [§7](#7-operators) |
| 9 | **Degree 1, always**: `*` needs a variable-free factor, `/` a variable-free divisor, `**` is refused. Bounds are narrower still — a name or a number, never arithmetic. | [§5](#5-expressions), [§2](#2-declarations) |
| 10 | **The operator set is closed.** Compositions go in `macros:`. | [§7](#7-operators) |

## 1. File shape

Eight top-level keys: `dimensions`, `parameters`, `variables`, `constraints`,
`objectives` (§2), `expressions`, `macros` (§3), `piecewise` (§4). The schema
accepts any subset, but `check`, `solve` and `write` require an objective —
there is nothing for the streaming lane to optimise without one.

**The schema is closed at every level.** An unrecognised key — top-level or
inside any declaration — is a load error naming the near miss (`unknown key
'boundz' … Did you mean 'bounds'?`). Ignoring it would let a typo change the
model: a dropped `bounds:` leaves a variable unbounded, a dropped `where:`
leaves it unmasked.

**Reading rules.** Booleans are YAML 1.2 (`true`/`false` only), everything else
1.1 — under 1.1 `on`/`off`/`yes`/`no`/`y`/`n` become booleans and silently
destroy dimension labels that are country codes, so `values: [no, se, on]` is
three labels here. The implicit timestamp (`2024-01-01`) and sexagesimal ints
(`12:30` → `750`) deliberately survive; the `dtype` guard below catches them
wherever they were not meant. A duplicate key is a load error naming both lines. `<<:` merge keys are
honoured, and a key the mapping declares itself overrides the merged value. The
document must be a mapping.

## 2. Declarations

**An empty dim list is the empty coordinate, everywhere it appears** — one
value for a parameter's `dims: []`, one column for a variable's `foreach: []`,
one row for a constraint's. Not a special case but the ordinary reading of a
product over nothing, whose unit is a single coordinate rather than none. So a
dummy dimension of size 1 is never how a scalar is written, and an objective —
scalar by definition — needs no `foreach` at all. One gap: a scalar **variable**
may not carry a `where` ([#340](https://github.com/FBumann/lpspec/issues/340)).
Put the condition on the constraints that use it.

**`dimensions`** — the master coordinate index. Every dimension named anywhere
must be declared. `dtype` ∈ {`float`, `int`, `str`, `datetime`}, default `str`.
`values` is a list or null; if null, coordinates must arrive via `sources=`
(`coords=` on the linopy lane), else loading fails. Every declared value must
be of the declared `dtype` — `values: [2024-01-01]` under the default
`dtype: str` is a load error, because YAML resolved it to a date and a date
does not join `'2024-01-01'` in the data.

`coords` declares non-index coordinates the dimension's labels carry — a
generator's bus, a line's endpoints, a snapshot's month — mapping each
coordinate name to **the dimension its values are labels of**. Written as a
list when the two names coincide, or as a mapping when they do not:

```yaml
dimensions:
  bus: {dtype: str}
  generator:
    coords: [bus]  # same as {bus: bus}
  line:
    coords: {from: bus, to: bus}  # two coordinates onto one dimension
```

The target must be a declared dimension, must not be the dimension carrying
the coordinate, and a coordinate must not be named after a *different*
dimension. A coordinate is single-valued per label, and its non-null values are
checked to be coordinates of the target once data is bound (§8) — that check is
what makes `group_sum` safe.

A coordinate may be **partial**: a null value says the label belongs to no
group, and `group_sum` places its terms nowhere. That is the same row-absence
idiom the language uses everywhere else for "not present" — a generator on no
bus, a line with one open end — and it is distinct from a *wrong* label, which
is still an error. Null means "no group"; an unknown non-null value is a typo. A dimension declaring `coords` needs an index source
carrying those columns; they are never inferred from the parameters that use
the dimension, because inferring them would let a mistyped label extend the
label space instead of being rejected.

**`parameters`** — declared shape only; data binds by name at run time (§8).
`dims` required (`[]` is a scalar); `dtype` ∈ {`float`, `int`, `bool`, `str`},
default `float`.

**`variables`**

| Field | Type | Default |
|---|---|---|
| `foreach` | list[str] | required — dim signature, one variable per coordinate |
| `where` | str or null | `null` — §6; variables exist only where true |
| `bounds.lower` / `.upper` | number or parameter name | `-inf` / `inf` |
| `binary`, `integer` | bool | `false`; not both |

Omitting a bound means unbounded on that side, as in
`linopy.Model.add_variables` — non-negativity is written, not assumed. Bounds
are a *narrower* language than expressions (a name or a number, never
arithmetic) and the error says so rather than reporting a parse failure;
expressions here are [#31](https://github.com/FBumann/lpspec/issues/31). A
bound parameter's dims must not exceed `foreach`.

**Equal bounds pin a variable**, which is how one declaration covers a quantity
that is a decision in one model and data in another: declare it as a variable
always, and bind `lower` and `upper` to the same value where it is fixed.
`rate - relmax * size <= 0` is then one equation whether `size` is chosen or
given, instead of a block per regime with pre-multiplied coefficients whose
names encode the regime rather than the quantity. Presolve fixes and substitutes
the pinned column, so the solver receives the LP the pre-multiplied form would
have produced; the cost is the columns before presolve. Two limits: a pinned
variable is still a variable, so `size * on` remains variable × variable and is
refused (§5), and it cannot appear in another variable's `bounds`, which take a
parameter or a number.

**`constraints`** — **one rule per block**: `foreach` (required), an optional
`where`, and one `expression` carrying exactly one of `<=`, `>=`, `==`. The
block's name *is* the constraint's name, which is what a row is read back by.
The LHS must involve at least one decision variable.

`foreach: []` is **one scalar row** — a single system-wide budget, where the
expression reduces every dim away. Nothing special: law 5 requires `dims(lhs) ∪
dims(rhs)` to *equal* `foreach`, and `sum(x, over=f) <= 120` has no free dims,
so `[]` is the signature that satisfies it.

Two regimes of one rule are two blocks, and each gets a name a reader chose
rather than a position in a list:

<!-- doctest: wrap=constraints -->
```yaml
storage_balance:
  foreach: [snapshot, storage]
  expression: soc == shift(soc, over=snapshot, by=1) * (1 - loss) + charge - discharge

storage_balance_initial:
  foreach: [snapshot, storage]
  where: "snapshot == 0"
  expression: soc == soc_initial
```

`shift` vacates the first snapshot and a vacated position is absent (§7), so
that row drops without a `where` saying so. Spelling the carry-over `edge=wrap` and
gating it with `where: "snapshot > 0"` builds the same rows here and a different
model on a horizon that does not start at 0 — the gate hardcodes the origin,
where the operator does not.

**`objectives`** — `sense` ∈ {`minimize`, `maximize`}, default `minimize`. An
objective is a scalar by definition, so **every dim the expression carries is
summed**; writing the sums out says nothing extra. Each *term* is summed over
the dims **that term** carries, and is not repeated because another term
carries a dim it does not: in `x * a + y * b` with `x, a` on `i` and `y, b` on
`j`, the objective has `|i| + |j|` summands, never `|i| · |j|`.

One `expression`, like a constraint — an objective was always one expression,
and now it says so in its shape. Declaring more than one objective is a load
error.

## 3. `expressions` and `macros`

Pure AST substitution before dispatch — neither backend ever sees one, so they
cost nothing and cannot make the lanes diverge. A named expression is a macro
with no formals.

```yaml
expressions:
  total_generation: sum(p, over=generator)
macros:
  weighted_sum:
    args: [array, weights]  # positional formals, default []
    kwargs: [over]  # keyword formals, default []
    template: sum(array * weights, over=over)
```

Both hold arithmetic (no comparison). Arguments expand before substitution
(call-by-value), so they may themselves use macros and named expressions.
Formals shadow model names inside a template but may not collide with a declared
**dimension**. Arity is checked per call site; cycles are reported with the
reference chain. Templates are schema-local, so every one is parsed and
name-checked at load time even if never called.

## 4. `piecewise`

N expressions jointly pinned to a breakpoint-indexed piecewise-linear curve,
mirroring `linopy.Model.add_piecewise_formulation`.

```yaml
piecewise:
  chp:
    over: bp  # breakpoint dimension
    links:
      - [power, power_bp]  # [expression, values-parameter]
      - [fuel, fuel_bp]
      - [heat, heat_bp]
    convex: false  # true: pure-LP convex hull, no binaries
    active: null  # optional gating expression: formulation pinned to 0

  # a two-link block may bound one side instead of pinning it
  fuel_cap:
    over: bp
    links:
      - [power, power_bp]
      - [fuel, fuel_bp, "<="]
```

*expression* is any affine expression (a bare variable name being the simplest);
*values* names a parameter carrying the `over` dim, so curves may vary along
other dims (per-generator, say); *sign* (`<=`/`>=`, at most one, only with
exactly two links) bounds the link instead of pinning it. Blocks expand **before
building** into plain variables and constraints via λ convex-combination —
weights in `[0,1]` with a convexity row, one link row per tuple, and unless
`convex: true` segment binaries with an adjacency row
`lam <= seg + shift(seg, over=bp, by=1, edge=0)`. Both lanes receive the identical expansion.

## 5. Expressions

```text
expression  ::= arithmetic | arithmetic COMPARATOR arithmetic
arithmetic  ::= atom | unary_op arithmetic | arithmetic binary_op arithmetic
             |  function_call | "(" arithmetic ")"
atom        ::= NUMBER | NAME
unary_op    ::= "+" | "-"       binary_op ::= "+" | "-" | "*" | "/" | "**"
COMPARATOR  ::= "<=" | ">=" | "=="
function_call ::= NAME "(" [pos_arg ("," pos_arg)*] ["," kwarg ("," kwarg)*] ")"
kwarg       ::= NAME "=" (arithmetic | NAME)
NAME        ::= [a-zA-Z][a-zA-Z0-9_]*
NUMBER      ::= integer | float | "inf" | ".inf"
```

Precedence, highest first: `**`, then `*` `/`, then binary `+` `-`, then unary
`+` `-`; parentheses override. Affinity is enforced — `*` needs at least one
variable-free factor, `/` a variable-free divisor that is a single factor rather
than a sum. **`**` parses but is not in the language**: both lanes reject it at
load time, so the refusal can name the operator and its rewrite. A variable base
breaks degree 1; over parameters alone it is data prep.

### 5.1 Name resolution

A **load-time pass** (`resolution.py`), not an evaluation-time lookup: parsers
emit `NameNode` tokens, the pass rewrites each into `VariableNode`, `ParameterNode`
or `DimensionNode`, so no
unresolved name crosses into a backend and no backend can hold its own opinion
about what a name means.

**One flat namespace** covers dimensions, parameters, variables, named
expressions, macros and built-in operators; a collision is a load error naming
both declarations. Ordered resolution with shadowing is wrong for a fail-loud
language: under it, declaring a parameter named `snapshot` would silently change
what an existing `where: "snapshot > 0"` means.

| Position | Legal kinds |
|---|---|
| expression (`p * cost`) | variable, parameter |
| dimension argument (`over=`, `into=`) | dimension |
| where string | parameter, dimension |
| `bounds.lower` / `.upper` | parameter name, or a number |
| `shift(x, over=d, by=n, edge=0)` — the `edge` key | `wrap`, or a number; never a dimension |

`edge` is the one keyword whose *key* is fixed rather than naming a dimension,
so a dimension called `edge` does not change what it means; the position takes
`wrap` or a number and nothing else.

A dimension in a value position is an error — it is a coordinate space, not
data. To use its coordinates as data, declare a parameter over it.

### 5.2 Dim algebra

Parameter `dims` and variable `foreach` are declared and dimension arguments are
name-checked, so **every node's dim set is computable before any data is bound**.
`dimensions.py` computes it at load time on the resolved AST, which is what makes
both lanes agree by construction.

| Node | Dim set | Error |
|---|---|---|
| number | `{}` | |
| parameter / variable | its `dims` / its `foreach` | |
| `-x`, `+x` | `dims(x)` | |
| `a + b`, `a * b`, `a / b` | `dims(a) ∪ dims(b)` | |
| `sum(x, over=d)` | `dims(x) − {d}` | if `d ∉ dims(x)` |
| `group_sum(x, over=d, by=c)` | `(dims(x) − {d}) ∪ {target(c)}` | unless `d ∈ dims(x)`, or `d` declares no coordinate `c` |
| `shift(x, over=d, by=n)` | `dims(x)` | if `d ∉ dims(x)` |

Binary operators **union**: an outer product is legitimate when the frame
declares the result. What must not be silent is the declaration disagreeing —
so a **constraint** requires `dims(lhs) ∪ dims(rhs)` to *equal* `foreach` (a
stray dim multiplies rows and an unused `foreach` dim repeats one row across
them, either way building a different model than the file reads as), while a
**where** predicate's dims and a **bound** parameter's dims must not exceed the
frame.

## 6. Absence

A coordinate where a **variable does not exist** — not a value and not a zero,
but a state the language tracks (law 6).

| construct | what is absent |
|---|---|
| `where:` on a variable | the variable, at the masked coordinates |
| `where:` on a constraint | the row |
| `shift(x, over=d, by=n)` with no `edge=` | the vacated edge coordinate (§7) |
| a null value in a dimension's `coords:` | that label's group membership (§2) |

**A sparse parameter table is not one of them.** Missing rows are compressed
encoding, and law 8 says what one reads as: the reading under which the missing
thing contributes nothing — or a refusal, where no such reading exists.

| position | a missing parameter row | why that reading |
|---|---|---|
| coefficient — `w * x` | zero: the term does not participate, the row survives | `0` is the identity of a sum, so the term contributes nothing |
| `where` operand | false | a coordinate whose data is missing is not one the model can claim exists |
| divisor — `x / d` | **refused** at bind *where the model divides by it* | nothing contributes nothing: `0` divides by zero, `1` rescales, dropping rewrites the constraint |
| `bounds:` | an error | nothing contributes nothing: unbounded is not bounded-at-zero |

### How absence travels

**Through arithmetic it spreads** (law 7), taking the row with it: `x + y >= 10`
is *no constraint* where `y` is masked, not `x >= 10`. Its asymmetry with the
table above is the whole hazard, in one example: `x - rel_max * size <= 0`
**loses the row** where the *variable* `size` is masked, and **keeps** it as
`x <= 0` where the *parameter* `rel_max` has no row — feasible, plausible, no
error. A missing correction term tightens in the safe direction and is a
legitimate idiom; a missing coefficient that *is* the bound rewrites what the
constraint says.

**Out of a reduction it does not** — `sum(x, over=d)` is defined when only some
of `d` exists, or one masked component would delete a system-wide accounting
row. So the two spellings below are different questions:

| spelling | sums over | with `y` absent at `f=b` |
|---|---|---|
| `sum(x + y, over=f)` | where the **summand** exists | `x[a] + y[a]` — `x[b]` goes with the absent `y[b]` |
| `sum(x, over=f) + sum(y, over=f)` | each operand over **its own** domain | `x[a] + x[b] + y[a]` |

*The total of the net where the net is defined*, against *the total in minus the
total out*. Rewriting the first into the second would read the absent `y[b]` as
a zero — the honest consequence of `+` being addition on a partial domain.

### Asking for the other reading

Each rule has a spelling for the opposite intent:

| you want | you write |
|---|---|
| the row kept, the missing term read as zero | two constraints under complementary `where` clauses |
| a vacated shift position to contribute | `shift(x, over=d, by=n, edge=0)` — the identity of *its* position (§7) |
| to test whether a variable exists here | its bare name in a `where` |
| a sparse coefficient to remove the row rather than zero the term | mask on it — `where: "rel_max"` |
| to divide by a parameter you only have some of | mask the row or the variable — `where: "d"`. The divisor is required where the division survives, not everywhere it is indexed |
| a bound only where the data has one | supply the missing value (`inf` is a value), or mask the variable — the two build **different models**, so neither is inferred |

**Only one of those is a fill** (law 8): the coordinate `shift` vacates is
*created by the operator*, so there is no row a caller could have supplied.
Everywhere else the value is expressible in the data, and §11 keeps it there —
`.fillna(inf)` for a bound is one line in the caller, over a table that is one
row per `foreach` coordinate anyway.

This is linopy's v1 arithmetic convention, which both lanes are built against;
`lpspec.linopy.semantics` is where the eager lane answers it.

### 6.1 Where strings

A boolean mask; true means "this coordinate exists". Semantics are **row
absence**, not zero-fill: a masked-out variable is not created, a masked-out
constraint row is not built.

```text
where_expr ::= atom | "NOT" where_expr | where_expr ("AND"|"OR") where_expr
            |  "(" where_expr ")"
atom       ::= NAME | NAME COMPARATOR value | "True" | "False"
COMPARATOR ::= "<=" | ">=" | "==" | "!=" | "<" | ">"
value      ::= NUMBER | NAME_OR_STRING
```

| Surface | Names a… | Meaning |
|---|---|---|
| `name` (bare) | parameter | defined: non-null **and** finite |
| `name` (bare) | variable | defined: the variable exists at this coordinate. The counterpart of the parameter row, and the way to say which coordinates the row-dropping rule above applies to |
| `name` (bare) | dimension | load error — true everywhere, so it reads as a condition and is not one; compare it instead |
| `name OP value` | parameter | element-wise, NaN → False. RHS is a literal number, or a bare name read as a string coordinate — a name that is *declared* is a load error instead (below) |
| `name OP value` | dimension | filter on the frame's own coordinate column |
| `AND` `OR` `NOT` | — | case-insensitive; `NOT` > `AND` > `OR` |
| `True` / `False` | — | literals; `True` ≡ no `where` |

Comparing two parameters is not in the language — precompute a boolean parameter
in data prep — and neither is comparing two dimensions. The string reading of an
RHS name is for names the model does *not* declare, which is how a string
coordinate is compared; a **declared** name on the RHS (parameter, variable or
dimension) is a load error naming the near miss, because reading it as text
would compare a coordinate column against another declaration's name and mask
everything out. An undeclared *bare* name is a load error on both lanes, and a
mask dim outside `foreach` is one too (§5.2).

## 7. Operators

The built-in set is **closed** — no Python registry — which is what makes both
lanes accept the same language and the differential tests an oracle rather than
a comparison of dialects. Dimension arguments are name-checked at load time:
`sum(p, over=snapshto)` is an error, not a no-op.

| Operator | Result | Notes |
|---|---|---|
| `sum(array, over=dim)` | `dim` collapses | `array` must carry `dim` |
| `group_sum(array, over=dim, by=coord)` | `over` → the dimension `coord` targets | `coord` is declared on `over` (§2); its values are the group labels, checked against the target dimension at bind time. The membership sum that makes topology data rather than structure; groups with no members contribute nothing |
| `shift(array, over=dim, by=n)` | value at *t−n* | vacated positions are **absent**: they propagate and drop the row (§6) |
| `shift(array, over=dim, by=n, edge=wrap)` | value at *t−n*, cyclic | coordinates fixed, values wrap; nothing is vacated |
| `shift(array, over=dim, by=n, edge=v)` | value at *t−n* | vacated positions contribute the number **`v`** instead, and the row survives (`0` for a sum, `1` for a product) |

**One operator, and `edge=` is the whole boundary question.** The three
policies are values of one keyword rather than two keywords that can
contradict each other, so "cyclic, and also fill what it vacates" has no
spelling to be refused — a cyclic map vacates nothing, and the surface says so
by construction.

`array` is any node of the right dim set, so `shift` re-indexes a
**parameter** as readily as a variable: `shift(dt, over=t, by=1, edge=0)` is the
previous snapshot's duration, and saves shipping a pre-shifted copy of a table
the model already has.

The position a shift leaves at the edge is **absent** in exactly §6's sense, so
an acyclic recurrence has no row at its first coordinate rather than a row
asserting the quantity starts at zero. An initial condition is then something
the model states, under a complementary `where`, rather than something the
language supplies unasked.

A numeric `edge=` asks for a value back instead, and it is a **number rather
than a flag because the identity is positional** (law 8): `lam <= seg +
shift(seg, over=bp, by=1, edge=0)` bounds the first breakpoint by the first
segment, where dropping the row would leave it unbounded; `x * shift(eff,
over=t, by=1, edge=1)` leaves the first coordinate governed by its own bound,
where `edge=0` would pin it. The library
cannot see which position it is in and the model can — which is v1's own reason
for refusing to fill on a caller's behalf.

The same law bounds what a numeric `edge` may be. Over an expression carrying a
**variable** the only representable value is `0`, since a vacated slot there
contributes no term at all and a nonzero one would be a constant standing where
a term was.

**A bare `shift` over a variable-free expression is a load error.** Absence is a
property of variables (§6); a parameter's missing row is a zero coefficient, so
there is no absence for the vacated slot to carry and inventing a value there is
what silently turned `x <= shift(dt, over=t, by=1)` into `x <= 0`. The error
names the three things it could have meant: `edge=0`, a `where` that masks the
coordinate out, or `edge=wrap` if the horizon is genuinely cyclic.

Anything composable out of these belongs in `macros:`. Math that is not sayable
at all goes to a declared `escape:` island
([#38](https://github.com/FBumann/lpspec/issues/38)): named in the file,
bounded by the preceding `where` mask, terminal (it yields a constraint, never a
sub-expression), and billed against a label budget before any Python runs.

## 8. Data binding

**Master coordinates** are resolved per dimension before any parameter loads,
highest precedence first:

1. a key in `sources` — a DataFrame carrying a column of that name, or a
   parquet path; first occurrence of each value is its position
2. `coords=` — anything `pd.Index()` accepts, or a DataFrame carrying the
   label column plus one column per declared coordinate (§2)
3. `values:` in the YAML
4. *streaming lane only* — derived from the parameter tables that carry the
   dim, as **sorted** distinct values

Step 4 is unavailable to a dimension declaring `coords` — it reads index
columns only, so it cannot supply a coordinate. Otherwise step 4 exists because
a dim some parameter already spans needs no second declaration, but it costs the *declared order*, which `shift` reads
positionally — so pass an explicit index whenever order matters. The linopy
lane has no step 4: a dimension with neither `coords=` nor `values:` raises
there. A dim that no source names and no parameter carries raises on both.

**Accepted per parameter** (declared `dims: [d1, d2]`), streaming lane: a
parquet path; any table exposing the Arrow PyCapsule protocol with columns
`d1, d2, value`; `int`/`float` for a 0-D parameter. `pd.Series` and
`xr.DataArray` keep their dims in an *index* rather than in columns, so they
are unwrapped first — but only if that library is already imported, never by
importing it.

Compat lane (`data=`): `int`/`float` as a scalar that broadcasts freely; `dict`
and `pd.Series` for 1-D (keys / index values become coordinates);
`pd.DataFrame` for 2-D (index name → `d1`, column name → `d2`); `xr.DataArray`
directly, with dim names a subset of the declared dims. `np.ndarray` and `list`
have no named axes, so only 0-D or 1-D matching one declared dim is accepted —
anything else is refused with a message asking for a named object.

Index names are optional but **binding**: an unnamed index binds positionally
to the declared `dims`, a named one binds by name in any order, and a name
outside the declared dims raises rather than being overwritten.

Coordinate values in the data must be a subset of the master coordinate; values
outside it raise rather than being dropped silently. Every declared parameter
must be provided, and every provided key must be declared — the YAML is the
source of truth. Validation order: dimension coords → parameter presence → dim
names → coordinate values → unknown keys.

The loader deliberately does **not** check that values are sensible, that a
parameter is used, or that coordinates *cover* the master index. Missing
coordinates produce no rows — sparse data gives sparse variables.

## 9. Errors

**Fail at load time, not at evaluation time.** Anything detectable before
building is detected before building; the worst error is an opaque xarray or
solver exception with no pointer back to a YAML declaration. Every message names
what went wrong, what to do about it, and where it helps, the valid options:

```text
Constraint 'balance', equation 0: 'p_charge' not found.
  Variables: ['p', 'soc']
  Parameters: ['p_max', 'load', 'efficiency']
Check for typos, or ensure 'p_charge' is declared.
```

A construct outside the language names the construct and its rewrite — never a
silent fallback, never a redirection to the other lane.

## 10. Python API

Five verbs — `check`, `load_schema`, `build`, `solve`, `write` — and the
exception tree rooted at `LinopyYamlError`: `LanguageError` (with `SchemaError`,
`DimensionError`, `PiecewiseExpansionError`) for the model, `DataError` for what
was bound to it.

```python
import lpspec as lps

lps.check('model.yaml')  # parse → validate → lower, no data bound
schema = lps.load_schema('model.yaml')  # MathSchema

result = lps.solve('model.yaml', sources, solver_options={'time_limit': 60})
result.status, result.termination_condition, result.objective
result.is_ok  # linopy's rollup: not an error, abort or refusal
result.has_primal  # narrower: are there values to read
result.primal('p')  # tidy frame (dims…, value) — the native shape
result.dual('power_balance')  # shadow prices, the same shape and the same join
result.to_pandas('p')  # the same, as a DataFrame
result.to_dataarray('p')  # the same, labelled: .sel / resample / plot
result.to_dataset()  # every variable by default; names for a subset
result.to_parquet(directory)  # streamed to disk, never through this process

lps.write('model.yaml', sources, 'model.lp')  # sink chosen by the suffix
```

**Nothing has to be released.** The built model is frames this process owns, so
`primal` and the `to_*` readers stay valid for as long as the `Result` does.
`close()` and the context-manager protocol exist to hand a large model back
early, not because forgetting them breaks anything. `lps.build` returns the
executor when one build should feed more than one sink:

```python
ex = lps.build('model.yaml', sources)
ex.write_lp('model.lp')
result = ex.solve()
```

What `sources` accepts is §8. Nothing on this path imports linopy, and `primal`
returns a `polars.DataFrame` — Arrow-backed, so it exports the same protocol the
loader recognises. `to_pandas` and `to_dataarray` are the bridges out and need
pandas / xarray, which ship with the `[linopy]` extra.

The only build knob is `coords`, shared by all three entry points.
**`solver_options` is separate and is not a build knob** — it is forwarded
verbatim to the solver, the shape linopy takes (`{"time_limit": 60,
"mip_rel_gap": 0.01}`); build knobs govern construction and never reach it.

**`is_ok` is not `has_primal`.** `is_ok` is linopy's rollup of the termination
condition; `has_primal` adds the solver's own verdict on whether an incumbent
exists, and it is what every reader gates on. They differ exactly when a run
stops early: a MIP that hits `time_limit` before finding any feasible point is
`ok` with nothing to read. Reading anyway raises `NoSolutionError`, and
`objective` is `nan`. `to_dataset` costs what it says — each variable arrives
dense over its own dims, so anything but a small model should name a subset or
use `to_parquet`. `dual` is the same label
join against the constraint's row frame, and **raises rather than returning
zeros** in either of the two ways it can come up empty: no values at all is
`NoSolutionError`, the gate `primal` passes through too, while a solve that
*did* leave values but no duals — any integer or binary variable makes them
undefined — raises `LinopyYamlError`, because the primals are still readable
and only this quantity is missing. Duals exist only on the `solver_direct`
path — a model written to LP and solved elsewhere never passes back through
here. Reduced costs and slacks ride the same join and are not exposed yet.
`.lp` is the only sink `write` supports today; `.mps` raises
`NotImplementedError`.

**Linopy shim** (`lpspec.linopy`, `[linopy]` extra) — two *pure producers*,
YAML in, model out, nothing retained:

```python
from lpspec import linopy as lpspec_linopy

m = lpspec_linopy.build('model.yaml', data={...}, coords={...})  # -> linopy.Model
lpspec_linopy.extend(m, 'ramp.yaml', data={...})  # mutates m in place
```

`build` returns a plain `linopy.Model` — no accessor, no attached schema, no
patched attributes — so nothing is lost across `pickle`, `deepcopy` or
`to_netcdf`; to inspect the math, re-read the file with `lps.load_schema`.
`extend` may reference variables already on the model (they come from the model
argument, not from Python-side history), while the YAML must still declare every
parameter *and dimension* it uses — the declaration is required, the `values:`
are not, since they can come from the model. Coords precedence for `extend`: the `coords=` kwarg, then
coords inferred from the model's variables, then `values:` in the YAML, then
error — a `values:` contradicting the model's existing coordinate is an error,
not a silent override. There is no `register()` decorator and no helper registry.

## 11. Out of scope

| Not here | Instead |
|---|---|
| time-series processing (resample, cluster, interpolate, align), file IO, units | data prep; pass a parameter |
| solver breadth | HiGHS via `solver_direct`, Gurobi planned on the same path, LP files for everything else ([#106](https://github.com/FBumann/lpspec/issues/106)) |
| SOS and indicator constraints | `piecewise:` (§4) covers SOS2's usual purpose; the streaming lane's default solver has no SOS or indicator concept at all, so this is a *sink capability* question rather than a language one — [#23](https://github.com/FBumann/lpspec/issues/23), ROADMAP Track 4 |
| multi-objective | one objective — declaring a second is a load error (§2); weight them into one expression |
| schema migrations | — |
| arbitrary array ops (`merge`, `reindex`, `apply_ufunc`) | data prep, or a declared `escape:` island — the closed AST is what makes streaming possible |
| filling a missing value (`.fillna`) | data prep, or a `where` if you meant the coordinate not to exist. In the language only where the data cannot reach — `shift(..., edge=)`, §6 |

Calliope's math language is a corpus we score coverage against, not a
specification we match; file portability is not a goal, and neither is
operation parity with xarray/pandas. A model built partly in Python has no
readable `.yaml` representation and will not get one: the *math* side is
feasible, but expression and where strings come back as anonymous arrays, so the
round-trip is functional and not reviewable — which is the whole point of the
file. Whether Python may *emit* declarations at all is a separate and open
question ([#381](https://github.com/FBumann/lpspec/issues/381)).
