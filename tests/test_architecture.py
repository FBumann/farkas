"""docs/ARCHITECTURE.md, enforced.

Each test encodes one hard rule from the architecture document, so the doc
cannot silently drift from the code. Static checks parse source with ``ast``
— they need no optional dependencies and run on a bare install.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).parent.parent
PKG = REPO / 'src' / 'lpspec'

FORBIDDEN_RUNTIME = {'linopy', 'xarray'}


def _in_linopy_lane(path: Path) -> bool:
    """The linopy/oracle lane — the ONLY modules allowed to import linopy or
    xarray at module level (they load only via ``import lpspec.linopy``).

    Structural, not a filename allowlist: membership is "lives under
    ``linopy/``". A new eager-lane module therefore cannot land outside the
    fence by being spelled differently.
    """
    return 'linopy' in path.relative_to(PKG).parts


def _module_level_imports(path: Path) -> set[str]:
    """Top-level (non-lazy, non-TYPE_CHECKING) imported root packages.

    Module-level ``try:`` blocks count. An optional-dependency guard is still
    a module-level import, and wrapping one must not evade this check —
    ``linopy/__init__.py`` uses exactly that pattern, so the rule has to see through it.
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
    """Hard rule 3: linopy is the eager/oracle lane only — never a runtime import."""
    offenders = {}
    for path in _all_modules():
        if _in_linopy_lane(path):
            continue
        bad = _module_level_imports(path) & FORBIDDEN_RUNTIME
        if bad:
            offenders[str(path.relative_to(PKG))] = sorted(bad)
    assert not offenders, (
        f'runtime modules import linopy-lane packages at module level: {offenders} '
        f'— make the import lazy or move the module into the linopy lane'
    )


#: Modules outside the linopy lane that may reach the oracle *lazily*, with
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
        if _in_linopy_lane(path) or path.name in LAZY_ORACLE_ALLOWED:
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
        f'modules outside the linopy lane reach the oracle lazily: {offenders} — '
        f'move the code to the linopy lane, or add it to LAZY_ORACLE_ALLOWED with a reason'
    )


#: Package modules the engine may import: dependency-free leaves that carry no
#: YAML, schema or AST knowledge. ``errors.py`` is one — without it there is no
#: single exception class a caller can catch across both lanes.
ENGINE_MAY_IMPORT = {'lpspec.errors'}


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
                        a.name.startswith('lpspec')
                        and not a.name.startswith('lpspec.relational')
                        and a.name not in ENGINE_MAY_IMPORT
                    )
                ]
            elif isinstance(node, ast.ImportFrom) and node.module:
                m = node.module
                if m.split('.')[0] in FORBIDDEN_RUNTIME | {'yaml'} or (
                    m.startswith('lpspec') and not m.startswith('lpspec.relational') and m not in ENGINE_MAY_IMPORT
                ):
                    bad.append(m)
        if bad:
            offenders[str(path.relative_to(PKG))] = sorted(set(bad))
    assert not offenders, f'engine reaches outside its subpackage: {offenders}'


#: What ``language/`` may reach: itself, and the same dependency-free leaves the
#: engine may reach. Both fences point at ``errors.py`` for the same reason —
#: one exception hierarchy, owned by neither side. Widening this is a decision,
#: exactly as widening ``ENGINE_MAY_IMPORT`` is.
LANGUAGE_MAY_IMPORT = ENGINE_MAY_IMPORT


def test_language_never_reaches_a_consumer():
    """Hard rule 1, the other direction: the waist is closed from the front.

    Hard rule 2 keeps the engine from seeing the schema or the AST. This is its
    mirror: what a model *means* may not depend on what any consumer does with
    it, so nothing under ``language/`` imports ``lowering``, ``piecewise``,
    ``sources``, ``api``, or the relational / linopy / typeset subpackages.

    That is what makes ``lps.check()`` a pass with no data and no plan, and a
    second consumer cheap rather than a second opinion. Membership is read off
    the path, so a new front-end module cannot land outside the fence by being
    spelled differently.
    """
    offenders = {}
    for path in (PKG / 'language').rglob('*.py'):
        if '__pycache__' in path.parts:
            continue
        bad = []
        for node in ast.walk(ast.parse(path.read_text())):  # lazy imports included — the rule is total
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.module
                else []
            )
            bad += [
                n
                for n in names
                if n.startswith('lpspec') and not n.startswith('lpspec.language') and n not in LANGUAGE_MAY_IMPORT
            ]
        if bad:
            offenders[str(path.relative_to(PKG))] = sorted(set(bad))
    assert not offenders, (
        f'the language reaches forward to a consumer: {offenders} — a front-end module '
        f'may not depend on what is done with the AST it produces'
    )


def test_expansion_has_no_mutable_module_state():
    """Hard rule 5: YAML files are self-contained — nothing importable may
    accumulate state that changes what a file means."""
    tree = ast.parse((PKG / 'language' / 'expansion.py').read_text())
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
    import lpspec.relational.plan as plan

    compiler_src = (PKG / 'relational' / 'compiler.py').read_text()
    for base in (plan.Expression, plan.Predicate):
        unhandled = [c.__name__ for c in base.__subclasses__() if f'plan.{c.__name__}' not in compiler_src]
        assert not unhandled, f'plan.{base.__name__} nodes unknown to the compiler: {unhandled}'


def test_both_lanes_implement_exactly_the_closed_helper_set():
    """Hard rule 3: one language, two lanes. A helper name the eager lane
    evaluates but the relational lane cannot lower (or vice versa) is a
    dialect split, and it would make the differential tests meaningless.

    Read statically: ``linopy/builder.py`` imports xarray at module level (it
    is linopy lane), and this check must still run on a bare install.
    """
    from lpspec.language.helpers import BUILTIN_NAMES

    tree = ast.parse((PKG / 'linopy' / 'builder.py').read_text())
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
    """No module is undocumented — but the doc need not be docs/ARCHITECTURE.md.

    A subpackage that grows a member per variant (one sink per module) would
    push its whole membership list into the top-level map, which is the thing
    that map exists *not* to be. A ``README.md`` beside the code counts
    instead: it is what you read when you open the directory, and it stays
    next to the thing it describes.
    """
    architecture = (REPO / 'docs/ARCHITECTURE.md').read_text()
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
        f'undocumented modules: {missing} — add each to docs/ARCHITECTURE.md, or to a '
        f'README.md in its own directory if it is one member of a family'
    )


def test_every_schema_model_is_strict():
    """A schema model that inherits BaseModel directly silently drops unknown
    keys, which turns a typo into a different model. Strictness lives on
    ``_StrictBlock``, so the check is that nothing bypasses it."""
    tree = ast.parse((PKG / 'language' / 'schema.py').read_text())
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


#: Every in-function ``lpspec`` import in the package, with why it is one.
#: A lazy import is a real tool here — it is how the one genuine cycle is
#: broken — which is exactly why the decorative ones had to go: a reader
#: cannot tell load-bearing from leftover if both are present.
DELIBERATE_LAZY_IMPORTS = {
    ('lowering.py', 'lpspec.piecewise'): (
        'formulations expand before lowering, and expanding needs the subset '
        'test that lowering defines — piecewise imports lowering at module '
        'level, so this direction has to stay lazy'
    ),
}


def test_lazy_intra_package_imports_are_all_declared():
    """Hard rule 1, mechanically: the layers are ordered, bar one declared edge.

    An undeclared in-function import is either a cycle nobody noticed or a
    leftover. Both are worth a line of explanation, so both fail here.
    """
    found = {}
    for path in _all_modules():
        tree = ast.parse(path.read_text())
        module_level = set()
        stack = list(tree.body)
        while stack:
            node = stack.pop()
            module_level.add(id(node))
            if isinstance(node, ast.Try):
                stack += [*node.body, *node.orelse, *node.finalbody]
                stack += [b for h in node.handlers for b in h.body]
            elif isinstance(node, ast.If):
                stack += [*node.body, *node.orelse]
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith('lpspec')
                and id(node) not in module_level
            ):
                found[(str(path.relative_to(PKG)), node.module)] = node.lineno

    undeclared = {k: v for k, v in found.items() if k not in DELIBERATE_LAZY_IMPORTS}
    assert not undeclared, (
        f'undeclared in-function imports {undeclared} — hoist them to module level, '
        f'or add them to DELIBERATE_LAZY_IMPORTS with the cycle they break'
    )
    stale = set(DELIBERATE_LAZY_IMPORTS) - set(found)
    assert not stale, f'DELIBERATE_LAZY_IMPORTS lists imports that no longer exist: {stale}'
