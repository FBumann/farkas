"""ARCHITECTURE.md, enforced.

Each test encodes one hard rule from the architecture document, so the doc
cannot silently drift from the code. Static checks parse source with ``ast``
— they need no optional dependencies and run on a bare install.
"""

from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).parent.parent / 'linopy_yaml'

FORBIDDEN_RUNTIME = {'linopy', 'xarray'}


def _in_compat_lane(path: Path) -> bool:
    """The compat/oracle lane — the ONLY modules allowed to import linopy or
    xarray at module level (they load only via ``import linopy_yaml.compat``).

    Structural, not a filename allowlist: membership is "lives under
    ``compat/``". A new eager-lane module therefore cannot land outside the
    fence by being spelled differently.
    """
    return 'compat' in path.relative_to(PKG).parts


def _module_level_imports(path: Path) -> set[str]:
    """Top-level (non-lazy, non-TYPE_CHECKING) imported root packages.

    Module-level ``try:`` blocks count. An optional-dependency guard is still
    a module-level import, and wrapping one must not evade this check —
    ``compat.py`` uses exactly that pattern, so the rule has to see through it.
    """
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    stmts = list(tree.body)  # module level only — function bodies are lazy
    while stmts:
        node = stmts.pop()
        if isinstance(node, ast.Import):
            found.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split('.')[0])
        elif isinstance(node, ast.Try):
            stmts.extend([*node.body, *node.orelse, *node.finalbody])
            for handler in node.handlers:
                stmts.extend(handler.body)
    return found


def _all_modules() -> list[Path]:
    return [p for p in PKG.rglob('*.py') if '__pycache__' not in p.parts]


def test_runtime_lane_never_imports_linopy_or_xarray():
    """Hard rule 3: linopy is compat/oracle only — never a runtime import."""
    offenders = {}
    for path in _all_modules():
        if _in_compat_lane(path):
            continue
        bad = _module_level_imports(path) & FORBIDDEN_RUNTIME
        if bad:
            offenders[str(path.relative_to(PKG))] = sorted(bad)
    assert not offenders, (
        f'runtime modules import compat-lane packages at module level: {offenders} '
        f'— make the import lazy or move the module into the compat lane'
    )


#: Modules outside the compat lane that may reach the oracle *lazily*, with
#: the reason. Being on this list is a deliberate exception, not a default —
#: an eager-only function living in a language module is what put
#: ``evaluate_where`` in ``where_parser.py`` for as long as it did.
LAZY_ORACLE_ALLOWED = {
    'piecewise.py': 'convex curvature validation needs xarray broadcast (issue #27: make it numpy-only)',
}


def test_lazy_oracle_imports_stay_on_the_allowlist():
    """Hard rule 3, the half a module-level check cannot see.

    A lazy ``import xarray`` inside a function is still eager-lane code, and
    it hides in a module the streaming lane imports. Every one has to be
    declared, so adding another is a decision rather than an accident.
    """
    offenders = {}
    for path in _all_modules():
        if _in_compat_lane(path) or path.name in LAZY_ORACLE_ALLOWED:
            continue
        tree = ast.parse(path.read_text())
        bad = set()
        for node in ast.walk(tree):  # anywhere, at any nesting
            if isinstance(node, ast.Import):
                bad |= {a.name for a in node.names if a.name.split('.')[0] in FORBIDDEN_RUNTIME}
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.split('.')[0] in FORBIDDEN_RUNTIME:
                bad.add(node.module)
        if bad:
            offenders[str(path.relative_to(PKG))] = sorted(bad)
    assert not offenders, (
        f'modules outside the compat lane reach the oracle lazily: {offenders} — '
        f'move the code to the compat lane, or add it to LAZY_ORACLE_ALLOWED with a reason'
    )


#: Package modules the engine may import: dependency-free leaves that carry no
#: YAML, schema or AST knowledge. ``errors.py`` is one — without it there is no
#: single exception class a caller can catch across both lanes.
ENGINE_MAY_IMPORT = {'linopy_yaml.errors'}


def test_engine_is_isolated():
    """Hard rule 2: the engine knows nothing about linopy, xarray or YAML.

    Enforced as "imports nothing from the package bar ENGINE_MAY_IMPORT",
    which is stricter than the written rule and deliberately so: the plan is
    fed to the engine, and keeping the import surface at zero is what leaves
    the subpackage extractable. Widening it is a decision — add the module to
    ENGINE_MAY_IMPORT with a reason, the way ``errors.py`` is there.
    """
    offenders = {}
    for path in (PKG / 'relational').rglob('*.py'):
        if '__pycache__' in path.parts:
            continue
        tree = ast.parse(path.read_text())
        bad = []
        for node in ast.walk(tree):  # include lazy imports — the rule is total
            if isinstance(node, ast.Import):
                bad += [
                    a.name
                    for a in node.names
                    if a.name.split('.')[0] in FORBIDDEN_RUNTIME | {'yaml'}
                    or (
                        a.name.startswith('linopy_yaml')
                        and not a.name.startswith('linopy_yaml.relational')
                        and a.name not in ENGINE_MAY_IMPORT
                    )
                ]
            elif isinstance(node, ast.ImportFrom) and node.module:
                m = node.module
                if m.split('.')[0] in FORBIDDEN_RUNTIME | {'yaml'} or (
                    m.startswith('linopy_yaml')
                    and not m.startswith('linopy_yaml.relational')
                    and m not in ENGINE_MAY_IMPORT
                ):
                    bad.append(m)
        if bad:
            offenders[str(path.relative_to(PKG))] = sorted(set(bad))
    assert not offenders, f'engine reaches outside its subpackage: {offenders}'


def test_expansion_has_no_mutable_module_state():
    """Hard rule 5: YAML files are self-contained — nothing importable may
    accumulate state that changes what a file means."""
    tree = ast.parse((PKG / 'expansion.py').read_text())
    mutable = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, (ast.Dict, ast.List, ast.Set)) or (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in {'dict', 'list', 'set'}
            ):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                mutable += [ast.unparse(t) for t in targets]
    assert not mutable, (
        f'expansion.py holds mutable module-level state {mutable} — '
        f'macros/expressions must live in the schema, not a registry'
    )


def test_every_plan_node_is_handled_by_the_compiler():
    """Two-tier economy: a primitive is not done until the engine consumes it.

    The compiler is the consumer — it is the module that turns plan nodes into
    SQL, so a node it does not mention has no relational meaning however much
    the executor moves around it. Grep-level drift alarm; the differential
    tests prove semantics.
    """
    import linopy_yaml.relational.plan as plan

    compiler_src = (PKG / 'relational' / 'compiler.py').read_text()
    for base in (plan.Expression, plan.Predicate):
        unhandled = [c.__name__ for c in base.__subclasses__() if f'plan.{c.__name__}' not in compiler_src]
        assert not unhandled, f'plan.{base.__name__} nodes unknown to the compiler: {unhandled}'


def test_both_lanes_implement_exactly_the_closed_helper_set():
    """Hard rule 3: one language, two lanes. A helper name the eager lane
    evaluates but the relational lane cannot lower (or vice versa) is a
    dialect split, and it would make the differential tests meaningless.

    Read statically: ``compat/builder.py`` imports xarray at module level (it
    is compat lane), and this check must still run on a bare install.
    """
    from linopy_yaml.helpers import BUILTIN_NAMES

    tree = ast.parse((PKG / 'compat' / 'builder.py').read_text())
    table = next(
        node.value for node in tree.body if isinstance(node, ast.AnnAssign) and ast.unparse(node.target) == '_HELPERS'
    )
    assert isinstance(table, ast.Dict)
    eager = {ast.literal_eval(k) for k in table.keys if k is not None}

    assert eager == set(BUILTIN_NAMES), (
        f'eager lane implements {sorted(eager)}, language declares {sorted(BUILTIN_NAMES)}'
    )

    # the relational lane spells its cases out in lowering.py rather than in a
    # table; every declared name must appear there as a lowering branch
    lowering_src = (PKG / 'lowering.py').read_text()
    missing = [name for name in BUILTIN_NAMES if f"'{name}'" not in lowering_src]
    assert not missing, f'built-in helpers with no lowering case: {missing}'


def test_every_module_is_documented_somewhere():
    """No module is undocumented — but the doc need not be ARCHITECTURE.md.

    A subpackage that grows a member per variant (one sink per module) would
    push its whole membership list into the top-level map, which is the thing
    that map exists *not* to be. A ``README.md`` beside the code counts
    instead: it is what you read when you open the directory, and it stays
    next to the thing it describes.
    """
    architecture = (PKG.parent / 'ARCHITECTURE.md').read_text()
    missing = []
    for path in _all_modules():
        name = path.name
        if name.startswith('_'):
            continue  # private plumbing (_notes) needs no doc entry
        if name == '__init__.py':
            continue
        local_readme = path.parent / 'README.md'
        documented = name in architecture or (local_readme.exists() and name in local_readme.read_text())
        if not documented:
            missing.append(str(path.relative_to(PKG)))
    assert not missing, (
        f'undocumented modules: {missing} — add each to ARCHITECTURE.md, or to a '
        f'README.md in its own directory if it is one member of a family'
    )


def test_every_schema_model_is_strict():
    """A schema model that inherits BaseModel directly silently drops unknown
    keys, which turns a typo into a different model. Strictness lives on
    ``_StrictBlock``, so the check is that nothing bypasses it."""
    tree = ast.parse((PKG / 'schema.py').read_text())
    loose = [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name != '_StrictBlock'
        and any(isinstance(b, ast.Name) and b.id == 'BaseModel' for b in node.bases)
    ]
    assert not loose, (
        f'schema models {loose} inherit BaseModel directly and so accept unknown keys — inherit _StrictBlock instead'
    )
