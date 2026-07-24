# linopy_yaml — Design Specification

**Status:** Draft for discussion
**Audience:** Contributors and collaborators

-----

## Table of Contents

1. [Overview](#1-overview)
1. [Relationship to linopy](#2-relationship-to-linopy)
1. [YAML Schema Reference](#3-yaml-schema-reference)
1. [Data Loading Contract](#4-data-loading-contract)
1. [Expression Language](#5-expression-language)
1. [Where Strings](#6-where-strings)
1. [Built-in Helper Functions](#7-built-in-helper-functions)
1. [Error Handling Philosophy](#8-error-handling-philosophy)
1. [Python API](#9-python-api)
1. [Out of Scope](#10-out-of-scope)
1. [Open Questions](#11-open-questions)
1. [Relational Backend](#12-relational-backend)

-----

## 1. Overview

`linopy_yaml` is a thin layer on top of [linopy](https://github.com/PyPSA/linopy) that lets users define optimisation problems in YAML rather than Python. A YAML file declares dimensions, parameters, variables, constraints, and an objective. At runtime, the user supplies data (pandas, numpy, or xarray objects) and receives a fully built `linopy.Model` ready to solve.

The core value proposition is **transparency and shareability**. A YAML math definition is readable without knowing Python, can be version-controlled, diffed, and shared with collaborators who don't write optimisation code. It separates *what the problem is* from *how it is built*.

### What it looks like

YAML definition (`dispatch.yaml`):

```yaml
dimensions:
  snapshot:
    dtype: int
  generator:
    values: [wind, solar, gas]

parameters:
  p_max:
    dims: [generator]
  load:
    dims: [snapshot]
  cost:
    dims: [generator]

variables:
  p:
    foreach: [snapshot, generator]
    where: "p_max > 0"
    bounds:
      lower: 0
      upper: p_max

constraints:
  power_balance:
    foreach: [snapshot]
    equations:
      - expression: sum(p, over=generator) == load

objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: sum(p * cost, over=generator)
```

Python call site:

```python
import linopy_yaml as ly
import pandas as pd

sol = ly.solve(
    "dispatch.yaml",
    sources={
        "p_max": pd.Series({"wind": 100, "solar": 60, "gas": 200}),
        "load":  pd.Series([80, 120, 150, 180, 140, 100], name="snapshot"),
        "cost":  pd.Series({"wind": 0, "solar": 0, "gas": 50}),
        "snapshot": pd.RangeIndex(6, name="snapshot"),
    },
)

print(sol.objective)
print(sol.primal("p"))
```

### Design principles

- **Explicit over inferred.** Dimension coordinates and parameter shapes are declared in the YAML. There is no guessing.
- **Fail early, fail clearly.** All validation happens at load time, before any linopy calls. Errors name the problem and say how to fix it.
- **Linopy-native output.** The result is a standard `linopy.Model`. Nothing is hidden or wrapped. Users can inspect variables, constraints, and the solution exactly as they would with hand-written linopy code.
- **No domain assumptions.** The package knows nothing about energy, transport, or any other domain. It is a general-purpose layer over linopy's API.

-----

## 2. Relationship to linopy

`linopy_yaml` does not need linopy to build or solve a model: the product path is YAML → AST → streaming engine → solver. linopy is reached only through the opt-in shim `linopy_yaml.compat` (the `[compat]` extra), which serves two narrow jobs:

1. **Python math the language cannot say** — build or extend a model in linopy, where arbitrary Python is available.
2. **Parity checking** — every language feature is differentially tested by running the same YAML + data through both the shim and the streaming engine. That is only meaningful because both accept *exactly* the same language.

Where it is used, it is a **pure consumer of linopy's public API**: it calls `model.add_variables()`, `model.add_constraints()`, and `model.add_objective()` — nothing else. It does not subclass linopy, patch it, or depend on its internals.

```python
from linopy_yaml import compat

m = compat.build("model.yaml", data={...}, coords={...})   # -> linopy.Model
compat.extend(m, "ramp.yaml", data={...})                  # YAML math onto an existing model
```

Both functions are **pure producers**: YAML goes in, a model comes out, and nothing is retained — no accessor, no session, no state attached to the model. The returned object is a plain `linopy.Model` and stands for itself. Consequences worth stating:

- A file's meaning never depends on what was loaded before it (hard rule 5). Every YAML declares the parameters it uses, and the caller supplies that data per call.
- `extend()` may reference variables already on the model — those come from the model argument, not from Python-side history — and infers coordinates from `model.variables.indexes`.
- Nothing is lost across `pickle`, `deepcopy`, or `to_netcdf`, because nothing is attached. To inspect the math, re-read the file with `ly.load_schema(path)`.

### Why a separate package?

- Keeps linopy's core dependency footprint lean (`pyparsing`, `pydantic` are not needed there).
- Allows independent versioning and iteration without coupling to linopy's release cycle.
- Different stability contracts: linopy's public API is stable; the YAML schema will evolve.

### Dependency surface

| Dependency  | Used for                                                               |
|-------------|------------------------------------------------------------------------|
| `linopy`    | Model, Variable, LinearExpression, add_variables/constraints/objective |
| `xarray`    | DataArrays, Dataset, broadcasting, merge                               |
| `pandas`    | Index objects, Series/DataFrame coercion                               |
| `numpy`     | Array operations, NaN handling                                         |
| `pyparsing` | Expression and where-string parsing                                    |
| `pydantic`  | YAML schema validation                                                 |
| `pyyaml`    | YAML file loading                                                      |

-----

## 3. YAML Schema Reference

A `linopy_yaml` YAML file has five top-level keys. All are optional except that a useful model will have at least `dimensions`, `parameters`, `variables`, and either `constraints` or `objectives`.

```yaml
dimensions:   ...   # master coordinate definitions
parameters:   ...   # named input data with declared shapes
variables:    ...   # decision variables
constraints:  ...   # linear constraints
objectives:   ...   # objective function(s)
```

### 3.1 `dimensions`

Declares the master coordinate index for each dimension. Every dimension referenced anywhere in the YAML must be declared here.

```yaml
dimensions:
  snapshot:
    dtype: int          # optional: float | int | str | datetime. Default: str
    values: null        # optional: list of values, or omit and pass via coords= at load time

  generator:
    values: [wind, solar, gas]
```

**Fields:**

| Field    | Type         | Default | Description                                                                                              |
|----------|--------------|---------|----------------------------------------------------------------------------------------------------------|
| `dtype`  | str          | `str`   | Expected dtype of the index. Used for coercion and validation. One of `float`, `int`, `str`, `datetime`. |
| `values` | list or null | `null`  | Coordinate values. If null, values must be supplied via the `coords=` argument at load time.             |

**Rules:**

- If `values` is null and no `coords` entry is provided at load time, loading raises immediately.
- All dimension names used in `foreach`, `parameters.dims`, `where` strings, or helper function calls must appear in `dimensions`.

### 3.2 `parameters`

Declares all named input data the model expects. Every parameter referenced in variable bounds, constraint expressions, or where strings must be declared here.

```yaml
parameters:
  p_max:
    dims: [generator]         # required: list of declared dimension names
    dtype: float              # optional: float | int | bool | str. Default: float

  efficiency:
    dims: []                  # empty list = scalar parameter

  is_storage:
    dims: [generator]
    dtype: bool

  load:
    dims: [snapshot]
```

**Fields:**

| Field  | Type      | Default  | Description                                                                                               |
|--------|-----------|----------|-----------------------------------------------------------------------------------------------------------|
| `dims` | list[str] | required | Dimensions this parameter is indexed over. Must all be declared in `dimensions`. Empty list means scalar. |
| `dtype` | str      | `float`  | Expected data type. Used for coercion after loading.                                                      |

**Rules:**

- Every declared parameter must be provided in `data=` at load time.
- Parameters cannot have dims that aren't in `dimensions`.

### 3.3 `variables`

Declares decision variables.

```yaml
variables:
  p:
    foreach: [snapshot, generator]   # required: dimensions to index over
    where: "p_max > 0"               # optional: boolean mask — only create variables where True
    bounds:
      lower: 0                       # number or parameter name. Default: 0
      upper: p_max                   # number or parameter name. Default: inf

  committed:
    foreach: [snapshot, generator]
    binary: true                     # optional: binary variable. Default: false

  unit_count:
    foreach: [generator]
    integer: true                    # optional: integer variable. Default: false
    bounds:
      lower: 0
      upper: 10
```

**Fields:**

| Field          | Type          | Default  | Description                                                                                                               |
|----------------|---------------|----------|---------------------------------------------------------------------------------------------------------------------------|
| `foreach`      | list[str]     | required | Dimension names to iterate over. Each combination is one variable.                                                        |
| `where`        | str or null   | `null`   | Where string (see [Section 6](#6-where-strings)). Variables are only created at coordinates where this evaluates to True. |
| `bounds.lower` | number or str | `0`      | Lower bound. Either a literal number or the name of a declared parameter.                                                 |
| `bounds.upper` | number or str | `inf`    | Upper bound. Either a literal number or the name of a declared parameter.                                                 |
| `binary`       | bool          | `false`  | If true, variable is binary (0/1). Bounds are ignored.                                                                    |
| `integer`      | bool          | `false`  | If true, variable is integer-valued.                                                                                      |

**Rules:**

- `binary` and `integer` cannot both be true.
- If `bounds.lower` or `bounds.upper` is a string, it must be the name of a declared parameter.
- A parameter used as a bound must be broadcastable onto the variable's `foreach` dimensions.

### 3.4 `constraints`

Declares linear constraints. Each constraint is a foreach loop with one or more equation expressions.

```yaml
constraints:
  power_balance:
    foreach: [snapshot]                 # required
    where: null                         # optional
    equations:
      - expression: sum(p, over=generator) == load

  ramp_up:
    foreach: [snapshot, generator]
    where: "snapshot > 0 AND ramp_max"
    equations:
      - expression: p - roll(p, snapshot=1) <= ramp_max

  storage_balance:
    foreach: [snapshot, storage]
    equations:
      - expression: soc == roll(soc, snapshot=1) * (1 - loss) + charge - discharge
        where: "snapshot > 0"           # per-equation where — narrows the foreach mask
      - expression: soc == soc_initial
        where: "snapshot == 0"
```

**Fields:**

| Field                     | Type        | Default  | Description                                                                                                                     |
|---------------------------|-------------|----------|---------------------------------------------------------------------------------------------------------------------------------|
| `foreach`                 | list[str]   | required | Dimensions to iterate over.                                                                                                     |
| `where`                   | str or null | `null`   | Mask applied to all equations in this constraint.                                                                                |
| `equations`               | list        | required | One or more equations. At least one required.                                                                                   |
| `equations[i].expression` | str         | required | The equation string (see [Section 5](#5-expression-language)). Must contain exactly one comparison operator (`<=`, `>=`, `==`). |
| `equations[i].where`      | str or null | `null`   | Additional mask for this equation only. ANDed with the constraint-level `where`.                                                |

**Rules:**

- Each equation produces one named constraint in the linopy model. If a constraint has multiple equations, they are named `constraint_name_0`, `constraint_name_1`, etc. If only one equation, it is named `constraint_name`.
- Each expression must contain exactly one of `<=`, `>=`, `==`. The operator separates LHS from RHS.
- The LHS must involve at least one decision variable (linopy cannot build a constraint that is purely parameter arithmetic).

### 3.5 `objectives`

Declares the objective function. Typically one, but multiple may be defined (only the last one added to the model takes effect unless using `extend()`).

```yaml
objectives:
  total_cost:
    sense: minimize             # minimize | maximize. Default: minimize
    equations:
      - expression: sum(p * cost, over=generator)
```

**Fields:**

| Field                     | Type | Default    | Description                                                                                    |
|---------------------------|------|------------|------------------------------------------------------------------------------------------------|
| `sense`                   | str  | `minimize` | Optimisation direction. One of `minimize`, `maximize`.                                         |
| `equations`               | list | required   | Currently only the first equation is used.                                                     |
| `equations[0].expression` | str  | required   | Arithmetic expression (no comparison operator). Must produce a scalar linopy LinearExpression. |


### 3.6 `expressions`

Named sub-expressions: reusable fragments of the expression language, referenced by name from any equation or other named expression.

```yaml
expressions:
  total_generation: sum(p, over=generator)
  net_cost: sum(p * cost, over=generator)

constraints:
  balance:
    foreach: [snapshot]
    equations:
      - expression: total_generation == load
```

Rules:

- Values are ordinary arithmetic expressions (no comparison operator).
- A name is spliced in as a parsed subtree wherever it appears — both backends see only the expanded core AST.
- Named expressions may reference each other; cycles are reported at load time with the reference chain.
- Names must not collide with declared parameters, variables, or dimensions.


### 3.7 `macros`

Parameterised expression templates, declared in the YAML itself — language, not code. Because macros live in the schema, a YAML file is fully self-contained: its meaning never depends on Python-side state.

```yaml
macros:
  weighted_sum:
    args: [array, weights]
    kwargs: [over]
    template: sum(array * weights, over=over)

objectives:
  total_cost:
    sense: minimize
    equations:
      - expression: weighted_sum(p, cost, over=generator)
```

**Fields (per macro):**

| Field      | Type | Default  | Description                                            |
|------------|------|----------|--------------------------------------------------------|
| `template` | str  | required | Arithmetic expression (no comparison operator).        |
| `args`     | list | `[]`     | Positional formal names.                               |
| `kwargs`   | list | `[]`     | Keyword formal names (e.g. `over` for dimension args). |

Semantics:

- Every call site is expanded into core AST before either backend sees the expression, so macros work identically on the eager and relational backends (and never force the backend router to fall back).
- Formal names shadow model names inside the template; all other names resolve against the model namespace.
- Arguments are expanded before substitution (call-by-value), so they may themselves use named expressions and macros.
- Arity is checked at each call; cycles are reported with the call chain.
- Names must not collide with parameters, variables, dimensions, named expressions, or helpers.
- **Validation is complete at load time**: because templates are schema-local, every template is parsed and name-checked against the schema even if the model never calls it.

-----

### 3.6 `piecewise`

N expressions jointly pinned to a breakpoint-indexed piecewise-linear curve,
mirroring `linopy.Model.add_piecewise_formulation`:

```yaml
piecewise:
  chp:
    over: bp                        # breakpoint dimension
    links:
      - [power, power_bp]           # [expression, values-parameter]
      - [fuel, fuel_bp, "<="]       # optional sign: bounded by the curve
      - [heat, heat_bp]
    convex: false                   # true: pure-LP convex hull, no binaries
```

Each link is `[expression, values, sign?]`: *expression* is any affine
expression string (a bare variable name being the simplest), *values* names a
parameter carrying the `over` dim (this link's breakpoint coordinates —
because they are parameters, curves may vary along other dims, e.g.
per-generator), and *sign* (`<=`/`>=`, at most one, only with exactly two
links) bounds the link instead of pinning it.

Blocks are **expanded before building** (`linopy_yaml.piecewise`) into plain
variables and constraints via the λ convex-combination method — λ weights in
`[0,1]` with a convexity row, one link row per tuple, and (unless `convex:
true`) segment binaries with an adjacency row `lam <= seg + shift(seg,
bp=1)`. Both backends receive the identical expanded schema; nonconvex
blocks make the model MILP (still relational-eligible via vtype).

-----

## 4. Data Loading Contract

### 4.1 Design rationale: why explicit is better than inferred

When building constraints from a YAML like `p <= p_max`, the evaluator needs to know the shape of `p_max`. Without that knowledge, errors surface late — deep inside the expression evaluator with cryptic xarray or linopy messages — rather than early at load time with a clear message pointing to the parameter name and the fix.

We considered several approaches to acquiring this shape information:

**Option A — Infer dims from data only.** Accept any named pandas/xarray object and infer dims from its axes. Works for well-named DataFrames but fails silently for scalars, dicts, and unnamed arrays, and cannot catch missing parameters upfront.

**Option B — Infer dims from math context only.** A parameter that appears in `foreach: [snapshot, generator]` must be broadcastable onto those dims. But "broadcastable" is not "equal to" — a scalar, a 1-D `[generator]` array, and a 2-D `[snapshot, generator]` array are all valid. Math context gives an upper bound on dims, not the exact shape.

**Option C — Explicit declaration in YAML (chosen).** Each parameter declares its `dims` in the YAML. The data loader validates that provided data matches. This eliminates ambiguity, enables immediate and precise error messages, and makes the YAML self-documenting as a data contract.

The tradeoff is verbosity: users must declare every parameter they intend to pass. This is consistent with the overall design philosophy — the YAML is meant to be a complete, readable specification of the model, not just a math shorthand.

### 4.2 Master coordinates

Before any parameter is loaded, a master coordinate index is assembled for every dimension. Sources, in order of precedence:

1. `coords=` kwarg passed to the loading call — highest priority, overrides everything.
1. `values:` declared in the YAML under `dimensions.dim_name`.
1. If neither is present for a declared dimension, loading raises immediately.

```python
# Values in YAML
dimensions:
  generator:
    values: [wind, solar, gas]

# Values via coords= (overrides YAML values if both present)
m = compat.build("model.yaml", data={...}, coords={
    "snapshot": pd.date_range("2024-01-01", periods=24, freq="h"),
})
```

The master coordinates are a `dict[str, pd.Index]`. They are passed to linopy's `add_variables()` as the `coords=` argument and used for mask broadcasting.

### 4.3 Accepted input types per parameter

For a parameter declared with `dims: [dim1, dim2]`:

| Python type      | How it is coerced                                                   | Constraints                                                                                 |
|------------------|---------------------------------------------------------------------|---------------------------------------------------------------------------------------------|
| `int` or `float` | Scalar `xr.DataArray`. Broadcasts freely over all dimensions.       | None.                                                                                       |
| `dict`           | `pd.Series` → `xr.DataArray`. Dict keys become coordinate values.   | Only for 1-D parameters. Dict keys must be a subset of the master coordinate for that dim.  |
| `pd.Series`      | `.to_xarray()`. Index name must match the declared dim.             | Only for 1-D parameters. Index values must be a subset of master coords.                    |
| `pd.DataFrame`   | `.stack()` → `.to_xarray()`. Index name → dim1, column name → dim2. | Only for 2-D parameters. Row/column values must be subsets of master coords.                |
| `xr.DataArray`   | Accepted directly. Dim names validated against declared dims.       | Dim names must be a subset of declared dims. Coord values must be subsets of master coords. |
| `np.ndarray`     | Requires explicit dim information — see below.                      | Must match declared shape exactly.                                                          |
| `list`           | Treated as `np.ndarray`.                                            | Same as ndarray.                                                                            |

**numpy arrays and lists** have no named axes, so the loader cannot determine which dimension each axis corresponds to. If a plain array is provided for a parameter with dims, it is accepted only if it is 0-D (scalar) or 1-D with length matching a single declared dim. Otherwise loading raises with a message asking the user to provide a named pandas or xarray object instead.

### 4.4 Validation rules

Validation happens in this order at load time. Each step fails immediately if its condition is not met.

**Step 1: Dimension coords**

- Every declared dimension has a value source (YAML or `coords=`).
- Error: `"Dimension '{name}' has no values. Declare them under 'dimensions.{name}.values' in the YAML or pass coords={{'{name}': [...]}}."`

**Step 2: Parameter presence**

- Every declared parameter is present in `data=`.
- Error: `"Parameter '{name}' is required but was not provided in data."`

**Step 3: Dimension names in provided data**

- For xr.DataArray: all dim names must be a subset of the declared dims for that parameter.
- Error: `"Parameter '{name}' has unexpected dimensions {unexpected}. Declared dims: {declared}."`

**Step 4: Coordinate values**

- All coordinate values present in the provided data must exist in the master coordinate for that dimension. Values not in the master are not silently dropped — they raise.
- Error: `"Parameter '{name}' has values in dimension '{dim}' that are not in the master coordinate: {unknown}.\nMaster '{dim}' coords: {master}"`

**Step 5: Unknown data keys**

- Keys in `data=` that are not declared parameters raise an error. The YAML is the source of truth.
- Error: `"The following data keys are not declared as parameters: {names}. Declare them under 'parameters:' in the YAML or remove them from data=."`

### 4.5 What the loader does NOT validate

- Whether the data's values are sensible (no range checks, no NaN warnings).
- Whether a parameter is actually *used* in the math (declared but unused is fine).
- Whether coordinate values in the data are a *complete* cover of the master coordinate. Missing values become NaN in the DataArray, which propagates into the where mask via `.notnull()` checks. This is intentional — sparse data produces sparse variables and constraints.

-----

## 5. Expression Language

Expressions appear in:

- `constraints.equations[i].expression` — must contain exactly one comparison operator
- `objectives.equations[i].expression` — arithmetic only, no comparison
- `variables.bounds.lower` / `bounds.upper` — currently only a name or number; full arithmetic expressions here are a v2 consideration

### 5.1 Syntax

```
expression  ::= arithmetic
             |  arithmetic COMPARATOR arithmetic

arithmetic  ::= atom
             |  unary_op arithmetic
             |  arithmetic binary_op arithmetic
             |  function_call
             |  "(" arithmetic ")"

atom        ::= NUMBER | NAME

unary_op    ::= "+" | "-"
binary_op   ::= "+" | "-" | "*" | "/" | "**"
COMPARATOR  ::= "<=" | ">=" | "=="

function_call ::= NAME "(" arg_list ")"
arg_list      ::= pos_arg ("," pos_arg)* ("," kwarg)*
               |  kwarg ("," kwarg)*
               |  empty
pos_arg       ::= arithmetic
kwarg         ::= NAME "=" (arithmetic | NAME)

NAME   ::= [a-zA-Z][a-zA-Z0-9_]*
NUMBER ::= integer | float | "inf" | ".inf"
```

### 5.2 Operator precedence

Standard mathematical precedence, highest to lowest:

| Priority    | Operators         | Associativity |
|-------------|-------------------|---------------|
| 1 (highest) | `**`              | Right         |
| 2           | `*`, `/`          | Left          |
| 3           | `+`, `-` (binary) | Left          |
| 4 (lowest)  | `+`, `-` (unary)  | Right         |

Parentheses override precedence in the usual way.

### 5.3 Name resolution

When a `NAME` token is encountered during evaluation:

1. Check decision variables (the linopy Model's variable store). If found, return the `linopy.Variable`.
1. Check parameters (the `xr.Dataset`). If found, return the `xr.DataArray`.
1. If neither, raise `NameError` with the name and the lists of available variables and parameters.

This ordering means a variable named `p` shadows a parameter named `p`. In practice, variable and parameter names should not overlap.

### 5.4 Type behaviour of arithmetic

The result type of arithmetic follows linopy's operator overloading:

| LHS                       | Operator           | RHS                          | Result                    |
|---------------------------|--------------------|------------------------------|---------------------------|
| `xr.DataArray`            | `+`, `-`, `*`, `/` | `xr.DataArray`               | `xr.DataArray`            |
| `linopy.Variable`         | `+`, `-`           | `xr.DataArray` or `Variable` | `linopy.LinearExpression` |
| `linopy.Variable`         | `*`                | `xr.DataArray`               | `linopy.LinearExpression` |
| `linopy.LinearExpression` | `+`, `-`           | anything                     | `linopy.LinearExpression` |

Broadcasting follows xarray semantics (dimension-name-based, not shape-based). A scalar DataArray broadcasts freely over any dimension.

### 5.5 Comparison operators

A comparison produces a `(lhs, op, rhs)` tuple consumed by the builder to call `model.add_constraints(lhs, op, rhs)`. The mapping from YAML operators to linopy signs:

| YAML | linopy `sign` argument |
|------|------------------------|
| `==` | `"="`                  |
| `<=` | `"<="`                 |
| `>=` | `">="`                 |

The LHS must be or reduce to a `linopy.LinearExpression`. The RHS must be or reduce to a numeric `xr.DataArray` or scalar. If this is violated, linopy will raise — the expression parser does not pre-validate this.

### 5.6 Examples

```yaml
# Simple capacity constraint
expression: p <= p_max

# Efficiency (parameter * variable)
expression: p_out == p_in * efficiency

# Sum over a dimension
expression: sum(p, over=generator) == load

# Time-coupled (rolling window)
expression: soc == roll(soc, snapshot=1) + charge - discharge

# Arithmetic on both sides
expression: p_in - p_out * (1 - loss) == 0

# Nested arithmetic in function
expression: sum(p * cost, over=generator) == total_cost_var
```

-----

## 6. Where Strings

Where strings produce boolean `xr.DataArray` masks. A `True` value means "include this coordinate combination". They appear on variables (restricting which variables are created) and constraints (restricting which constraints are built).

### 6.1 Syntax

```
where_expr  ::= atom_where
             |  "NOT" where_expr
             |  where_expr "AND" where_expr
             |  where_expr "OR" where_expr
             |  "(" where_expr ")"

atom_where  ::= NAME                          # existence check
             |  NAME COMPARATOR value         # comparison
             |  "True" | "False"              # boolean literals

COMPARATOR  ::= "<=" | ">=" | "==" | "!=" | "<" | ">"
value       ::= NUMBER | NAME_OR_STRING
```

`AND`, `OR`, `NOT` are case-insensitive.

### 6.2 Semantics

**Plain name** (`"p_max"`): Evaluates to True wherever the parameter is defined (non-null) and finite. Equivalent to `p_max.notnull() & (p_max != inf) & (p_max != -inf)`. If the parameter does not exist in the dataset, evaluates to scalar False.

**Comparison** (`"p_max > 0"`): Evaluates the comparison element-wise. NaN values propagate as False.

**Boolean operators**: Standard boolean logic. `AND` has higher precedence than `OR`. `NOT` has highest precedence.

**Boolean literals**: `True` and `False` (case-insensitive). `True` is equivalent to no where string.

### 6.3 Interaction with foreach

The where mask is evaluated against the parameter dataset (which has the master coordinates) and then broadcast onto the `foreach` grid. If a mask dimension is not in `foreach`, it is reduced by `any()` over that dimension before broadcasting.

For variables, the mask is passed directly to linopy's `mask=` argument. For constraints, the mask restricts which constraint rows are built.

### 6.4 Examples

```yaml
# Only create variables where p_max is defined and positive
where: "p_max > 0"

# Only where both parameters are defined
where: "p_max AND ramp_max"

# Exclude a specific snapshot (e.g. for time-coupling constraints)
where: "snapshot > 0"

# Combine conditions
where: "p_max > 0 AND NOT is_must_run"

# Always include
where: null   # omit entirely, or write: "True"
```

-----

## 7. Built-in Helper Functions

Helper functions are called inside expressions to perform operations that cannot be expressed with arithmetic alone.

### 7.1 `sum(array, over=dim)`

Sums an array or expression over a dimension.

```yaml
expression: sum(p, over=generator) == load
expression: sum(p * cost, over=generator)   # arithmetic in positional arg
```

| Argument | Type                  | Description                |
|----------|-----------------------|----------------------------|
| `array`  | arithmetic expression | The expression to sum.     |
| `over`   | dimension name        | The dimension to sum over. |

If the array does not have the named dimension, it is returned unchanged (no error).

Works with both `xr.DataArray` and `linopy.Variable`/`LinearExpression` (calls `.sum(dim)` on the underlying object).

### 7.2 `roll(array, dim=n)`

Shifts an array along a dimension by `n` positions (wrapping). Used for time-coupling constraints where a variable at time `t` depends on its value at `t-1`.

```yaml
# soc at t depends on soc at t-1
expression: soc == roll(soc, snapshot=1) + charge - discharge
```

| Argument | Type             | Description                                      |
|----------|------------------|--------------------------------------------------|
| `array`  | component name   | The array or variable to shift.                  |
| `dim=n`  | keyword, integer | The dimension to roll over and the shift amount. |

The shift is applied with `roll_coords=False` (coordinates stay fixed; values wrap). For time-coupling constraints where the first snapshot should be handled separately, use a `where` string to exclude it:

```yaml
constraints:
  storage_balance:
    foreach: [snapshot, storage]
    equations:
      - expression: soc == roll(soc, snapshot=1) + charge - discharge
        where: "snapshot > 0"
      - expression: soc == soc_initial
        where: "snapshot == 0"
```

### 7.3 `shift(array, dim=n)`

Non-cyclic counterpart of `roll`: the value at *t−n*, with vacated positions
contributing **zero** instead of wrapping. Use for acyclic recurrences —
e.g. storage that starts empty:

```yaml
expression: soc == shift(soc, snapshot=1) + charge - discharge
```

On the eager backend this is linopy/xarray `.shift()` (zero fill); on the
relational backend it is the same ord-join as `roll` without the modulo —
out-of-range rows simply don't join (row absence = zero contribution).

### 7.4 `group_sum(array, mapping, into=dim)`

Sums an array or expression through a **mapping parameter**, replacing the
mapping's dimension with a group dimension. This is the membership-sum needed
for network models: "sum the generators at each bus".

```yaml
# gen_bus: dims [generator], dtype str — maps each generator to its bus
expression: group_sum(p, gen_bus, into=bus) == load
```

| Argument  | Type                  | Description                                          |
|-----------|-----------------------|------------------------------------------------------|
| `array`   | arithmetic expression | The expression to sum. Must carry the mapping's dim. |
| `mapping` | parameter name        | 1-D parameter whose values are the group labels.     |
| `into`    | dimension name        | The resulting group dimension.                       |

The mapping's dimension is summed out; the result has dimension `into` with
the mapping's values as coordinates. On the eager backend this is linopy's
`.groupby()`; on the relational backend it is a join + GROUP BY.

Note: for the eager backend, every value of `into` that appears in the
constraint's `foreach` should be covered by the mapping — groups absent from
the mapping produce no output rows. The relational backend treats absent
groups as zero contribution.

### 7.5 The helper set is closed

There is **no Python helper registry**. The built-ins above are the whole set, and both lanes therefore accept exactly the same language — which is what makes the differential tests an oracle rather than a comparison of two dialects (ARCHITECTURE.md, hard rule 3).

Two routes replace it, in this order:

1. **`macros:` (§3.7)** for anything expressible as a composition of built-ins. Macros are parameterised, schema-local, expanded to core AST before dispatch, and cost nothing on either lane.

   ```yaml
   macros:
     weighted_sum:
       args: [array, weights]
       kwargs: [over]
       template: sum(array * weights, over=over)
   ```

2. **A declared `escape:` island** ([#38](https://github.com/FBumann/linopy-yaml/issues/38)) when the math is not sayable in the language at all. Unlike a registered helper — which read like a built-in on the page and left the file dependent on invisible Python state — an escape names its module path in the YAML, is bounded by the `where` mask that precedes it, is terminal (it yields a constraint, never a sub-expression), and is billed against a label budget before any Python runs.

-----

## 8. Error Handling Philosophy

**Fail at load time, not at evaluation time.** Every error that can be detected before building the linopy model should be detected before building the linopy model. The worst errors are those that surface as opaque xarray or linopy exceptions with no indication of which YAML declaration caused them.

**Name the problem and say how to fix it.** Every error message includes:

1. What went wrong (the specific parameter, dimension, or expression).
1. What the user needs to do to fix it.
1. When helpful, what valid options look like.

### 8.1 Error message templates

**Missing dimension values:**

```
Dimension 'snapshot' has no values.
Declare them under 'dimensions.snapshot.values' in the YAML
or pass coords={'snapshot': [...]}.
```

**Missing required parameter:**

```
Parameter 'load' is required but was not provided in data.
Add 'load' to the data= argument.
```

**Parameter with unexpected dims:**

```
Parameter 'p_max' has unexpected dimensions {'carrier'}.
Declared dims: ['generator'].
Either update the declaration or reshape your data.
```

**Parameter coordinate not in master:**

```
Parameter 'p_max' has values in dimension 'generator' that are not in the master coordinate: ['nuclear'].
Master 'generator' coords: ['wind', 'solar', 'gas']
```

**Undeclared dimension in foreach:**

```
Variable 'p' references undeclared dimension 'carrier'.
Declare it under 'dimensions:' in the YAML.
```

**Unknown name in expression:**

```
'p_charge' not found in expression 'p_charge - p_discharge'.
  Variables:  ['p', 'soc']
  Parameters: ['p_max', 'load', 'efficiency']
Check for typos, or ensure 'p_charge' is declared as a variable or parameter.
```

**Unknown helper function:**

```
Unknown helper function 'weighted_sum' in expression 'weighted_sum(p, over=generator)'.
Available: ['group_sum', 'roll', 'shift', 'sum']
Define 'weighted_sum' as a macro under 'macros:' if it composes built-ins; if the math is
not sayable in the language, use a declared escape.
```

-----

## 9. Python API

The product surface is the native, linopy-free API. The compat shim is opt-in
and exists for Python math the language cannot say, and for parity checking.

### 9.1 Native API (`linopy_yaml`)

```python
import linopy_yaml as ly

ly.check("model.yaml")                       # validate, no data needed
schema = ly.load_schema("model.yaml")        # MathSchema
sol = ly.solve("model.yaml", sources={...})  # build + stream + solve
ly.write_lp("model.yaml", "model.lp", sources={...})
```

`sources` maps parameter and dimension names to parquet paths, pandas objects,
or scalars. Nothing in this path imports linopy.

### 9.2 Compat shim (`linopy_yaml.compat`, `[compat]` extra)

Two functions, both **pure producers** — YAML in, model out, nothing retained:

```python
from linopy_yaml import compat

m = compat.build("model.yaml", data={...}, coords={...})   # -> linopy.Model
compat.extend(m, "ramp.yaml", data={...})                  # mutates m in place
```

| Parameter | Type             | Description |
|-----------|------------------|-------------|
| `path`    | `str` or `Path`  | Path to the YAML file. |
| `data`    | `dict` or `None` | Parameter data. Keys are parameter names as declared in *this* YAML. See [Section 4.3](#43-accepted-input-types-per-parameter) for accepted value types. |
| `coords`  | `dict` or `None` | Dimension coordinate values. Anything accepted by `pd.Index()`. Overrides `values:` declared in the YAML. |

`build()` returns a plain `linopy.Model` — no accessor, no attached schema, no
patched attributes. It stands for itself: solve, inspect, pickle, or
`to_netcdf` it exactly like a hand-written model. To inspect the math, re-read
the file with `ly.load_schema(path)`.

`extend()` adds variables, constraints, and/or objectives to an existing
model. Expressions may reference variables already on the model — those come
from the model argument, not from Python-side history — and the YAML must
declare every parameter it uses, with this call supplying that data.

**Coords precedence for `extend()`** (highest first):

1. `coords=` kwarg to this call
2. coords inferred from the model's existing variables
3. `values:` declared in this YAML
4. error if none of the above resolve a referenced dim

If the YAML declares `values:` for a dim the model already has, they must
match. Silent override would hide real bugs.

**Raises:** `ValueError` with a descriptive message for any validation
failure; `pydantic.ValidationError` if the YAML structure is invalid.

### 9.3 No helper-registration API

Removed. There is no `register()` decorator and no helper registry: the built-in set is closed so that both lanes accept the same language (§7.5, ARCHITECTURE.md hard rule 3). Compositions go in `macros:`; unsayable math goes in a declared `escape:` island ([#38](https://github.com/FBumann/linopy-yaml/issues/38)).

-----

## 10. Out of Scope

**Time series processing.** Resampling, clustering, interpolation, and alignment of time series data are not handled by `linopy_yaml`. Users should preprocess their data before passing it in.

**Data loading from files.** The package does not read CSV, Parquet, NetCDF, or any other file formats. Users load their data into pandas/xarray objects using whatever tools they prefer, then pass those objects in.

**Solver configuration.** Solver selection, options, and result parsing are deliberately thin: the native path exposes what the streaming sinks support (see [#28](https://github.com/FBumann/linopy-yaml/issues/28)); the compat shim leaves solving to linopy entirely.

**SOS constraints.** Not exposed through the YAML interface. (Piecewise-linear relationships *are* — see §3.6 `piecewise` — via λ-formulation expansion; a native SOS2 stream is tracked in [#23](https://github.com/FBumann/linopy-yaml/issues/23).)

**Multiple objectives / multi-objective optimisation.** Only one objective is added to the linopy model. Defining multiple objectives in YAML is not an error, but only the last one takes effect.

**Schema migrations.** No tooling is provided for migrating YAML files between versions of the schema.

**LaTeX rendering.** Not in v1. Now a committed direction rather than a maybe: it is an AST consumer with no backend tax — see [ROADMAP.md](ROADMAP.md) Track 3.

**Feature parity with other declarative math languages.** Calliope's math language (and the Calliope-derived port on linopy's `feature/declarative-yaml-interface` branch) is a **corpus we score coverage against, not a specification we match**. Constructs of theirs that fall outside the expressive ceiling in [ARCHITECTURE.md](ARCHITECTURE.md#the-expressive-ceiling) are out of scope by construction — and that ceiling admits operators they do not expose. Portability of files between the two is explicitly not a goal.

**Operation parity with xarray / pandas.** The language is not an array-programming API with YAML syntax. Arbitrary array manipulation (`merge`, `reindex`, `apply_ufunc`, resampling) is unbounded as a target and would destroy the closed-AST property that makes streaming possible. Such work belongs in data prep — computing a *parameter* in Python is unrestricted and costs the streaming path nothing. Where the custom logic must operate on *variables*, the bounded route is a declared `escape:` island ([#38](https://github.com/FBumann/linopy-yaml/issues/38)): visible in the file, restricted to the `where`-masked slice, terminal (it yields a constraint, never a sub-expression), and billed against a label budget.

-----

## 11. Open Questions

### Resolved

**Q1: Package name.** → `linopy-yaml` (Python import: `linopy_yaml`).

**Q2: Sub-expressions.** → Deferred to v2. Not needed for v1.

**Q4: `bounds` as full expressions.** → Deferred. Interactions with linopy's internals make this complex.

**Q5: Where string dimension comparisons.** → Implemented. The where parser checks dimension names when a name is not found as a parameter.

**Q6: Validation strictness.** → Unknown data keys raise an error. The YAML is the source of truth.

**Q7: Parameter defaults.** → Removed. Every parameter must be provided in `data=`. Keeps the loader simple and explicit.

### Open

**Q3: Indexed access (array slicing).**
Selecting a component at a fixed coordinate, and at a coordinate looked up through a mapping parameter (the *gather* whose adjoint is `group_sum`). Needed for terminal conditions, multi-period linking, and all network math beyond a nodal balance — DC power flow is unwritable without it. Both forms are pointwise and admissible under the expressive ceiling; scheduled as [ROADMAP.md](ROADMAP.md) Track 1 items 1–2. **Open only on surface syntax**: bracket slicing (`p[bus=gen_bus]`) puts the dimension in a kwarg key, where a macro cannot parameterise it, so a helper form keeping the dim in a value position is preferred.

**Q8: Should `.yaml` ever become a complete representation of the model?**
Currently `.yaml` covers only the YAML-managed portion. A future version could intercept `add_variables()` and `add_constraints()` calls and synthesise schema entries from their arguments. Investigation shows this is feasible on the *math* side — those calls carry all the structurally needed information (coords, bounds as DataArrays, `LinearExpression` coefficients, masks). What is lost is the **human-readable layer**: expression strings (`sum(p * cost, over=generator)`), where strings (`"p_max > 0"`), and bound parameter names become anonymous arrays. The result would be a **functional round-trip** (enough to rebuild an identical model) but not a **readable round-trip** (regenerating clean YAML); the latter would require linopy itself to carry expression provenance. Whether functional round-trip alone is worth the wrapping complexity is undecided. The clean alternative is to leave `.yaml` as "YAML-managed portion only" permanently. Tracked in [issue #3](https://github.com/FBumann/linopy-yaml/issues/3).

-----

## 12. Relational Backend

*Status: phase 2 (in design/implementation). Phase-1 spike results and
operational findings live in `scratch/relational_spike/README.md`.*

### 12.1 Why

linopy's memory problems are structural: the eager xarray data model
materialises dense, NaN-padded arrays (with a `_term` dimension) at every
operator, so build peak RSS is O(dense dim product). A YAML math spec is a
closed AST known before any data is touched — which makes it legal to compile
the whole model to a logical query plan and execute it relationally.

**Primary invariant: the full model is never held in this package's process
memory at any point** — not as dense arrays, not as a full CSR. Every stage
streams under a configured memory budget. The solver's own internal copy is
the only irreducible full-model residency.

**Scope (decided 2026-07-24): a streaming compiler for large pure-affine
models — not a general replacement for the eager builder.** linopy's
trajectory is toward mutable, transformable models with solver-native
constructs (piecewise formulations, SOS and indicator constraints,
dualization, in-place updates). The eager builder, as a pure consumer of
linopy's public API, inherits all of that for free; a flat
``(col, row, coeff)`` streamer inherits none of it.

**Superseded in part (549c055, and again by the P1 decision):** the two lanes
are still not fast-vs-slow versions of the same thing, but the relationship
inverted. The streaming engine is the product lane for models declared in
YAML; the compat lane is the product lane for YAML math attached to a
`linopy.Model` that already exists in memory (structurally eager — see
ARCHITECTURE.md hard rule 3). There is **no automatic routing and no
fallback**: both lanes accept the same language, the entry point picks the
lane, and a construct outside the language is a load error naming its
rewrite. Solver-native constructs linopy has and the sinks do not (SOS,
indicator) are therefore *sink-bounded* for both lanes — tracked as
[#23](https://github.com/FBumann/linopy-yaml/issues/23), not reachable by
falling back.

### 12.2 Architecture

```
data sources (parquet / arrow / pandas)      YAML math spec
                    │                             │ parse (existing)
                    │                             ▼
                    │                        typed AST ──────────────┐
                    │                             │ (phase 3)        │ (today)
                    │                             ▼                  ▼
                    │                     logical-plan IR       eager builder
                    │                             │             (xarray → linopy.Model)
                    │                             ▼                  │
                    └──────────────► relational executor             ▼
                                     (duckdb, phase 2;         linopy writers / solve
                                      in-memory, phase 4)      = correctness oracle
                                              │
                              ┌───────────────┼──────────────────┐
                              ▼               ▼                  ▼
                        lp_file sink      mps sink       solver_direct sink
                        (portability,                    (COO/CSR batches →
                         differential                     HiGHS / Gurobi)
                         oracle)                                 │
                                                                 ▼
                                                     solution tables (label join)
                                                       → parquet / pandas
```

Boundaries:

- **The AST is the only contract** between the YAML layer and the engine. The
  engine (`linopy_yaml.relational`) must not import the eager builder, and
  engine-internal naming encodes neither "duckdb" nor "yaml" — the durable
  idea is relational LP construction. (The engine may later be extracted as
  its own package once proven.)
- **The IR is the seam between language and execution.** Plans can be built in
  Python without YAML (phase 2), lowered from the AST (phase 3), and executed
  by different engines (duckdb now, in-memory later) and different sinks.
- **The eager linopy path stays.** It is the correctness oracle (differential
  tests: same model through both backends must produce equivalent solves) and
  it remains the right tool for interactive/incremental use. The relational
  path is batch build-and-solve; incremental model editing is out of scope.
- In the `solver_direct` path, linopy is not in the loop at all: the solver is
  driven through its own API (highspy / gurobipy), and solution read-back is a
  join of the solver's label-indexed arrays against the label tables.

### 12.3 Data model: tidy tables

Everything is a table with named coordinate columns:

| thing | columns |
|---|---|
| parameter | `(dim₁, …, dimₖ, value)` — k = 0 is a scalar |
| variable frame | `(dims…, var_label)` — one row per **existing** variable |
| linear expression | `(frame dims…, var_label, coeff)` + constant part `(frame dims…, const)` |
| constraint rows | `(row, sense, rhs)` |
| coefficient matrix | `(row, col, coeff)` — COO, `col` = `var_label` |

Masks are **row absence**: a variable excluded by `where` simply has no row.
No NaN sentinels, no `-1` labels. Broadcasting is a join; `sum(over=dim)`
drops coordinate columns (final canonicalisation groups by `(row, col)` and
sums coefficients); `group_sum` joins a mapping parameter and replaces the
source dim with the target dim. Labels are dense `0..n-1` by construction
(partition-wise `ROW_NUMBER` over the masked coord product), so `var_label`
**is** the solver column index and `row` the solver row index — no remapping.

### 12.4 IR node set (v0)

Declarations (frozen dataclasses in `linopy_yaml/relational/ir.py`):

- `Program(parameters, variables, constraints, objective)`
- `ParameterDecl(name, dims)` — table shape only; actual data is bound at
  execution time via a source registry (`name → parquet path | DataFrame`)
- `VariableDecl(name, dims, where, lower, upper)` — bounds are constant
  expressions (`Const` / `Param` arithmetic)
- `ConstraintDecl(name, dims, lhs, sense, rhs)` — `sense ∈ {==, <=, >=}`;
  both sides are affine expressions, the executor normalises constants to the
  right-hand side
- `ObjectiveDecl(sense, expr)` — remaining dims are implicitly summed

Affine expressions:

- `Const(value)`, `Param(name)`, `Var(name)`
- `Neg(x)`, `Add(a, b)`, `Mul(a, b)` — at least one factor of `Mul` must be
  variable-free
- `Sum(x, over)` — reduce named dims
- `GroupSum(x, mapping, into)` — sum through a mapping parameter
  (`mapping: dims → group value`), producing dim `into`

Predicates (for `where`): `Cmp(param, op, value)`, `And`, `Or`, `Not`.

This covers the v0 language subset (foreach, where, arithmetic, sum,
group_sum, roll, shift, comparison). Quadratic is out of scope.

**The IR is affine-by-design — decided, not provisional.** No node introduces
variables or constraints as a side effect of an expression. Formulations
are model *transformations*, not expressions: piecewise enters via
schema-level expansion (§3.6); SOS and indicator remain eager-only. If they ever come to the streaming path, they enter as a
distinct expansion stage that emits new variable/constraint declarations
*before* affine compilation — never as expression nodes — and only once the
sink has the corresponding native streams (§12.6). Reimplementing linopy's
reformulation passes (e.g. SOS big-M linearization) inside the IR is
explicitly rejected: that would duplicate the library this package consumes.
Variable *types* are not formulations: binary/integer are supported (a
``vtype`` column on ``cols``, LP ``binary``/``general`` sections, HiGHS
integrality), which makes basic MILP relational-eligible. Semi-continuous is
the remaining planned vtype extension.

Piecewise is implemented as *schema-level expansion* (§3.6): a
``piecewise:`` block expands into ordinary variable/constraint declarations
(λ convex-combination over a breakpoint dimension, binaries via vtype,
adjacency via ``shift``) so **both backends receive identical affine
declarations** and stay differential-testable. Formulations never enter as
IR expression nodes. See ``examples/piecewise.yaml`` for per-generator
curves. Convex piecewise costs are also expressible with no machinery at
all — the epigraph pattern in ordinary affine YAML (kept as a tested
pattern in ``tests/test_piecewise_convex.py``; automating it is issue #23's
``method: lp``).

### 12.5 Execution requirements (phase-1 spike, corrected after phase 3)

1. **Chunk only what cannot spill.** duckdb's joins and plain numeric hash
   aggregates spill under `memory_limit` on their own — coefficient assembly
   needs no chunking (verified: a single-shot `GROUP BY` over 35.6M term rows
   runs at a 256 MB cap). Exactly two operations need hand-managed
   partitioning: *label assignment* (a global `ROW_NUMBER` window
   materialises its input — use per-chunk `ROW_NUMBER` + a running offset,
   one generic mechanism every operator inherits) and the *LP-text
   `string_agg`* (string aggregates don't spill; a fixed conservative chunk
   size lives only in the debugging/portability sink). Future IR operators
   are classified by **coordinate locality**: pointwise (joins, scaling,
   masks, group_sum) and bounded-halo (roll: t±k — safe because terms join
   the global variable table, so a chunk referencing t−1 from the previous
   chunk just works) compose freely under the label partitioner; genuinely
   global operators (running sums, normalisations) are rejected at lowering
   with a rewrite hint (running sum → state-variable recurrence, which is
   bounded-halo). duckdb's spill coverage widens every release — remaining
   workarounds sit behind the executor interface and can be deleted as the
   engine catches up.
2. The duckdb database must be **file-backed** with a temp directory, so the
   buffer pool spills under `memory_limit`.
3. Output line/row order inside a sink section is free — labels are carried in
   the data — so no global sorts are needed (`preserve_insertion_order=false`).
   The `solver_direct` sink needs `ORDER BY row` for batching; that sort
   spills.
4. Benchmarks: runtime is measured untracked (memray slows duckdb ~8×); peak
   RSS via `/usr/bin/time -l` (or `ru_maxrss` of an isolated pass) is the gate
   metric; memray is for attribution only.

### 12.6 Sinks

- `lp_file` — streaming `COPY` per section, parts concatenated. Portability
  and the differential-test oracle format.
- `mps` — same mechanics, later.
- `solver_direct` — the end state. Stream `cols(col, lb, ub, obj_coeff)` and,
  ordered by `row`, Arrow record batches of `A(row, col, coeff)` split on row
  boundaries into batched solver calls (HiGHS `addCols`/`addRows`, Gurobi
  `addMVar`/`addMConstr`). Peak ≈ duckdb `memory_limit` + one Arrow batch +
  the solver's own model; float→text→parse disappears entirely. Full-CSR
  fallbacks violate the primary invariant and are last resorts.

**The sink is capped, explicitly.** Today it expresses columns with bounds,
objective coefficients, and integrality (continuous / binary / integer
vtypes), affine rows, and COO coefficients — nothing else. SOS sets and
indicator/general constraints have no stream. The documented upgrade path is
five streams — ``cols`` (gaining a semi-continuous threshold), ``rows``,
``A``, ``sos_sets``, ``genconstr`` — recorded here so the gap is a stated
design bound, not a surprise at implementation time. Anything a stream cannot carry routes to
the eager builder (§12.8).

### 12.7 Phase gates

1. ✅ Spike: hand-written SQL, dispatch model — peak RSS flat at the budget
   (0.49 GB vs 6.6 GB at 35.6M vars; 107M vars in 0.57 GB), ~2× runtime,
   differential-equivalent to 8.9M vars.
2. IR + duckdb executor, plans constructed in Python. Gate: **two real models
   round-trip through solve** (dispatch + a multi-bus transport model
   exercising `group_sum`), differential against the eager builder.
3. YAML → IR lowering behind the `_eval_ast` seam, v0 subset only.
4. In-memory executor for the same IR (folding in the CSR deferred-groupby
   prototype) so small models skip duckdb.

### 12.8 Backend eligibility and automatic fallback

Backend selection is the router's job, not the user's. A schema is
**relational-eligible** iff it lowers to the IR:
``linopy_yaml.router.relational_eligibility(schema)`` returns ``None`` on
success or the first lowering error — verbatim, with its context — as the
ineligibility reason. ``select_backend(schema)`` wraps this in an explicit
choice object.

Everything outside the streaming subset (custom helpers, ``**``,
where-comparisons on dimensions — and formulations like SOS if they enter
the YAML language) automatically routes to the eager builder with a stated
reason. The relational backend is an
optimization that must fall back; it is never a constraint on what the
language can express. The differential oracle (same YAML through both
backends must agree) is what keeps the fast lane honest.
