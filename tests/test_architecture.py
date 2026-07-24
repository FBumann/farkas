"""ARCHITECTURE.md, enforced.

Each test encodes one hard rule from the architecture document, so the doc
cannot silently drift from the code. Static checks parse source with ``ast``
— they need no optional dependencies and run on a bare install.
"""

from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).parent.parent / 'linopy_yaml'

#: The compat/oracle lane — the ONLY modules allowed to import linopy/xarray
#: at module level (they load only via `import linopy_yaml.compat`).
COMPAT_LANE = {'builder.py', 'loader.py', 'compat.py'}

FORBIDDEN_RUNTIME = {'linopy', 'xarray'}


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
        if path.name in COMPAT_LANE:
            continue
        bad = _module_level_imports(path) & FORBIDDEN_RUNTIME
        if bad:
            offenders[str(path.relative_to(PKG))] = sorted(bad)
    assert not offenders, (
        f'runtime modules import compat-lane packages at module level: {offenders} '
        f'— make the import lazy or move the module into the compat lane'
    )


def test_engine_is_isolated():
    """Hard rule 2: the relational engine imports nothing from the package
    outside its own subpackage — the IR is fed to it, it never reaches out."""
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
                    or (a.name.startswith('linopy_yaml') and not a.name.startswith('linopy_yaml.relational'))
                ]
            elif isinstance(node, ast.ImportFrom) and node.module:
                m = node.module
                if m.split('.')[0] in FORBIDDEN_RUNTIME | {'yaml'} or (
                    m.startswith('linopy_yaml') and not m.startswith('linopy_yaml.relational')
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


def test_every_ir_expr_node_is_handled_by_the_executor():
    """Two-tier economy: a primitive is not done until the executor consumes
    it. Grep-level drift alarm — the differential tests prove semantics."""
    import linopy_yaml.relational.ir as ir

    executor_src = (PKG / 'relational' / 'executor.py').read_text()
    unhandled = [cls.__name__ for cls in ir.Expr.__subclasses__() if f'ir.{cls.__name__}' not in executor_src]
    assert not unhandled, f'ir.Expr nodes unknown to the executor: {unhandled}'

    unhandled_pred = [cls.__name__ for cls in ir.Pred.__subclasses__() if f'ir.{cls.__name__}' not in executor_src]
    assert not unhandled_pred, f'ir.Pred nodes unknown to the executor: {unhandled_pred}'


def test_both_lanes_implement_exactly_the_closed_helper_set():
    """Hard rule 3: one language, two lanes. A helper name the eager lane
    evaluates but the relational lane cannot lower (or vice versa) is a
    dialect split, and it would make the differential tests meaningless.

    Read statically: ``builder`` imports xarray at module level (it is compat
    lane), and this check must still run on a bare install.
    """
    from linopy_yaml.helpers import BUILTIN_NAMES

    tree = ast.parse((PKG / 'builder.py').read_text())
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


def test_architecture_doc_mentions_every_module():
    """ARCHITECTURE.md's module map stays complete (its own first paragraph)."""
    doc = (PKG.parent / 'ARCHITECTURE.md').read_text()
    missing = []
    for path in _all_modules():
        name = path.name
        if name.startswith('_'):
            continue  # private plumbing (_notes) needs no doc entry
        if name == '__init__.py':
            continue
        if name not in doc:
            missing.append(str(path.relative_to(PKG)))
    assert not missing, f"modules absent from ARCHITECTURE.md's map: {missing}"
