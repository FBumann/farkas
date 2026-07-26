"""The examples in the docs, checked against the code.

Every example here was wrong at some point: ``ly.write_lp`` never existed, a
dimension index was passed as a bare ``RangeIndex`` where the streaming lane
wants a ``coords=`` entry, the ``piecewise:`` block carried a sign on three
links while the prose two lines below said a sign needs exactly two, and four
module docstrings leaked the executor by never closing the ``Solution``. Three
separate hand sweeps found three separate batches, which is the argument for
this file: an example nobody runs is a claim nobody checked.

Coverage cannot silently drop. Every fenced ``python`` and ``yaml`` block in
the tracked docs must be handled by one of the tests below, or carry an
explicit ``<!-- doctest: skip -->``; a new block that fits neither fails
:func:`test_every_block_is_covered` rather than being quietly ignored.

Annotations go in an HTML comment on the line before the fence, so they are
invisible in rendered markdown:

    <!-- doctest: wrap=constraints -->   nest the block under that schema key
    <!-- doctest: skip -->               excluded, and the reason belongs in a comment
"""

from __future__ import annotations

import ast
import re
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, NamedTuple, get_args

import pytest
import yaml

import linopy_yaml as ly
from linopy_yaml.relational.executor import DuckdbExecutor, Solution
from linopy_yaml.schema import MathSchema

try:
    from linopy_yaml import compat
except ModuleNotFoundError:
    # Bare install, no [compat] extra. The rest of this file is linopy-free and
    # must still run on the native lane, so the module cannot skip itself the
    # way tests/oracle.py does — only the checks that need compat step aside,
    # and they do it by skipping rather than by quietly checking less.
    compat = None

REPO = Path(__file__).resolve().parent.parent
TRACKED = ['README.md', 'SPEC.md', 'ARCHITECTURE.md', 'ROADMAP.md']

# Names an example may dot into, and the object that decides what is valid.
# Anything else (pd, np, network, ...) is external and not our contract.
ROOTS: dict[str, Any] = {
    'ly': ly,
    'compat': compat,
    'sol': Solution,
    'ex': DuckdbExecutor,
    'schema': MathSchema,
}

# Every root an example may name, whether or not this install can resolve it.
# Recognising an example must not depend on the extras: a compat example is
# still a compat example on a bare install, it just cannot be name-checked.
ROOT_NAMES = frozenset(ROOTS)
ROOTS = {name: obj for name, obj in ROOTS.items() if obj is not None}


def _unresolvable(code: str) -> set[str]:
    """Roots this example names that the install cannot supply."""
    return {root for root in ROOT_NAMES - set(ROOTS) if f'{root}.' in code}


_EXTRA = 'needs the [compat] extra to check {}'

_FENCE = re.compile(
    r'(?:<!--\s*doctest:\s*(?P<note>[^>]*?)\s*-->\s*\n)?```(?P<lang>python|yaml)\n(?P<code>.*?)```',
    re.DOTALL,
)


class Block(NamedTuple):
    doc: str
    lang: str
    index: int
    code: str
    note: str
    line: int

    @property
    def where(self) -> str:
        return f'{self.doc}:{self.line} ({self.lang} block #{self.index})'


def _blocks(lang: str | None = None) -> list[Block]:
    out: list[Block] = []
    for doc in TRACKED:
        text = (REPO / doc).read_text()
        counters: dict[str, int] = {}
        for m in _FENCE.finditer(text):
            got = m.group('lang')
            i = counters.get(got, 0)
            counters[got] = i + 1
            out.append(
                Block(
                    doc=doc,
                    lang=got,
                    index=i,
                    code=m.group('code'),
                    note=(m.group('note') or '').strip(),
                    line=text.count('\n', 0, m.start()) + 1,
                )
            )
    return [b for b in out if lang is None or b.lang == lang]


def _public(obj: Any) -> set[str]:
    """Attribute names an example may use — dataclass fields included, since a
    field with no default is not a class attribute and ``dir`` misses it."""
    names = {n for n in dir(obj) if not n.startswith('_')}
    if is_dataclass(obj):
        names |= {f.name for f in fields(obj) if not f.name.startswith('_')}
    return names


# --------------------------------------------------------------------------
# python blocks
# --------------------------------------------------------------------------


@pytest.mark.parametrize('block', _blocks('python'), ids=lambda b: b.where)
def test_python_block_parses(block: Block) -> None:
    if block.note == 'skip':
        pytest.skip('explicitly skipped')
    try:
        ast.parse(block.code)
    except SyntaxError as exc:  # pragma: no cover - only on a broken doc
        pytest.fail(f'{block.where} is not valid Python: {exc}')


@pytest.mark.parametrize('block', _blocks('python'), ids=lambda b: b.where)
def test_python_block_uses_real_api(block: Block) -> None:
    """Every ``ly.x`` / ``sol.x`` an example shows must exist.

    This is the check that would have caught ``ly.write_lp``, which was
    documented for months and never existed.
    """
    if block.note == 'skip':
        pytest.skip('explicitly skipped')
    if missing := _unresolvable(block.code):
        pytest.skip(_EXTRA.format(sorted(missing)))
    tree = ast.parse(block.code)
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            continue
        root = node.value.id
        if root not in ROOTS:
            continue
        if node.attr not in _public(ROOTS[root]):
            bad.append(f'{root}.{node.attr}')
    assert not bad, (
        f'{block.where} uses names that do not exist: {sorted(set(bad))}. Fix the example, or the API it documents.'
    )


def test_readme_example_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The front-door example must actually solve, and produce the number the
    README claims it produces."""
    yaml_blocks = [b for b in _blocks('yaml') if b.doc == 'README.md']
    py_blocks = [b for b in _blocks('python') if b.doc == 'README.md']
    model = next(b for b in yaml_blocks if '# dispatch.yaml' in b.code)
    script = next(b for b in py_blocks if 'ly.solve' in b.code)

    (tmp_path / 'dispatch.yaml').write_text(model.code)
    monkeypatch.chdir(tmp_path)

    ns: dict[str, Any] = {}
    exec(compile(script.code, 'README.md', 'exec'), ns)

    sol = ns['sol']
    assert sol.status == 'Optimal'

    # the README states the objective in a trailing comment; keep them in sync
    claimed = re.search(r'#\s*([0-9]+\.?[0-9]*)\s*$', script.code, re.MULTILINE)
    assert claimed, 'README example no longer states its objective in a comment'
    assert sol.objective == pytest.approx(float(claimed.group(1))), (
        f'README claims objective {claimed.group(1)}, run produced {sol.objective}'
    )


# --------------------------------------------------------------------------
# yaml blocks
# --------------------------------------------------------------------------


def _entry_model(section: str) -> Any:
    """The per-entry model behind a schema section, e.g. constraints -> ConstraintDef."""
    args = get_args(MathSchema.model_fields[section].annotation)
    return args[1] if len(args) == 2 else None


@pytest.mark.parametrize('block', _blocks('yaml'), ids=lambda b: b.where)
def test_yaml_block_validates(block: Block) -> None:
    """A YAML example must be a thing the schema accepts.

    Whole-section blocks go through ``MathSchema`` — including ``piecewise:``,
    which is why this catches a sign on three links. A ``wrap=`` block shows a
    single entry of a section and deliberately omits the declarations around
    it, so it is checked against that section's own model: its *shape* is our
    claim, its cross-references are not.
    """
    if block.note == 'skip':
        pytest.skip('explicitly skipped')

    doc = yaml.safe_load(block.code)
    assert isinstance(doc, dict), f'{block.where} is not a YAML mapping'

    if block.note.startswith('wrap='):
        section = block.note.removeprefix('wrap=')
        assert section in MathSchema.model_fields, f'{block.where}: wrap={section!r} is not a schema section'
        model = _entry_model(section)
        for name, entry in doc.items():
            try:
                model.model_validate(entry)
            except Exception as exc:
                pytest.fail(f'{block.where}: entry {name!r} does not validate:\n{exc}')
        return

    try:
        MathSchema.model_validate(doc)
    except Exception as exc:
        pytest.fail(f'{block.where} does not validate:\n{exc}')


# --------------------------------------------------------------------------
# the anti-rot guard
# --------------------------------------------------------------------------


def test_every_block_is_covered() -> None:
    """A new example must be checkable or explicitly skipped — never ignored."""
    unhandled = []
    for block in _blocks():
        if block.note == 'skip' or block.note.startswith('wrap='):
            continue
        if block.lang == 'python':
            continue  # every python block is parsed and name-checked
        keys = yaml.safe_load(block.code)
        if not isinstance(keys, dict) or not set(keys) <= set(MathSchema.model_fields):
            unhandled.append(block.where)
    assert not unhandled, (
        'these YAML blocks are neither whole schema sections nor annotated, so '
        'nothing checks them:\n  ' + '\n  '.join(unhandled) + '\n'
        'Add <!-- doctest: wrap=<section> --> or <!-- doctest: skip --> above the fence.'
    )


# --------------------------------------------------------------------------
# module docstrings — where the executor leak actually lived
# --------------------------------------------------------------------------

DOCSTRING_MODULES = ['linopy_yaml/__init__.py', 'linopy_yaml/api.py', 'linopy_yaml/compat.py']


def _docstring_examples(path: Path) -> list[str]:
    """Indented runs inside a module docstring that use our API.

    A run must *parse* once it mentions a known root — prose that merely looks
    indented is ignored, but an example is never allowed to be unparseable.
    """
    tree = ast.parse(path.read_text())
    doc = ast.get_docstring(tree) or ''
    runs, current = [], []
    for line in doc.splitlines():
        if not line.strip() or line.startswith('    '):
            current.append(line)
            continue
        if current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)

    out = []
    for run in runs:
        text = '\n'.join(run).strip('\n')
        if not text.strip():
            continue
        dedented = '\n'.join(ln.removeprefix('    ') for ln in text.splitlines())
        if any(f'{root}.' in dedented for root in ROOT_NAMES):
            out.append(dedented)
    return out


class Example(NamedTuple):
    module: str
    index: int
    code: str

    @property
    def where(self) -> str:
        return f'{self.module} (docstring example #{self.index})'


def _docstring_cases() -> list[Example]:
    """One case per example, so an install that cannot check one of them says
    so about that example rather than about the whole module."""
    return [
        Example(module, i, code)
        for module in DOCSTRING_MODULES
        for i, code in enumerate(_docstring_examples(REPO / module))
    ]


@pytest.mark.parametrize('module', DOCSTRING_MODULES)
def test_module_documents_its_api(module: str) -> None:
    """A module docstring that stops showing its API is a doc regression the
    per-example tests below cannot see — they would just collect nothing."""
    assert _docstring_examples(REPO / module), f'{module}: no API example found in the module docstring'


@pytest.mark.parametrize('example', _docstring_cases(), ids=lambda e: e.where)
def test_docstring_example_uses_real_api(example: Example) -> None:
    try:
        tree = ast.parse(example.code)
    except SyntaxError as exc:
        pytest.fail(f'{example.where} is not valid Python: {exc}\n{example.code}')
    # syntax is checked above on every install; only the name check below needs
    # the object behind the root, so only that part stands down
    if missing := _unresolvable(example.code):
        pytest.skip(_EXTRA.format(sorted(missing)))
    bad = [
        f'{n.value.id}.{n.attr}'
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id in ROOTS
        and n.attr not in _public(ROOTS[n.value.id])
    ]
    assert not bad, f'{example.where} uses names that do not exist: {sorted(set(bad))}'


@pytest.mark.parametrize('module', DOCSTRING_MODULES)
def test_docstring_examples_close_the_solution(module: str) -> None:
    """``ly.solve`` hands back a live duckdb executor. An example that binds it
    without a ``with`` teaches a leak — which is exactly what three of these
    docstrings did."""
    for code in _docstring_examples(REPO / module):
        tree = ast.parse(code)
        managed = {
            item.optional_vars.id
            for node in ast.walk(tree)
            if isinstance(node, ast.With)
            for item in node.items
            if isinstance(item.optional_vars, ast.Name)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            call = node.value
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr not in {'solve', 'build'} or not isinstance(call.func.value, ast.Name):
                continue
            if call.func.value.id != 'ly':
                continue
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            assert names & managed, (
                f'{module}: `{", ".join(sorted(names))} = ly.{call.func.attr}(...)` binds a live '
                f'executor outside a `with` block — the example leaks it. Use `with ... as`, '
                f'or show an explicit `.close()`.'
            )


def test_tracked_docs_exist() -> None:
    """Renaming a doc must not silently drop its examples from the sweep."""
    missing = [d for d in TRACKED if not (REPO / d).is_file()]
    assert not missing, f'tracked docs missing (update TRACKED): {missing}'
    assert _blocks('python'), 'no python examples found — the regex has drifted'
    assert _blocks('yaml'), 'no yaml examples found — the regex has drifted'
