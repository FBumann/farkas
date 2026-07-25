"""Reading YAML while keeping track of where each declaration came from.

``yaml.safe_load`` discards positions the moment a document parses, so every
error after the parse — a schema-shape complaint, an unparseable expression, an
unresolved name — can name the *declaration* but not the line. This module
composes the node tree once, records ``start_mark`` for every key and sequence
item into a side table keyed by document path, and then constructs the plain
Python document from the same node tree. One parse, plain ``dict``/``str``, no
loader wrappers leaking downstream.

Two YAML 1.1 defects are fixed in the same loader, since it is the only place
that sees the nodes:

- **1.2 booleans.** ``on``/``off``/``yes``/``no``/``y``/``n`` are ordinary
  dimension labels, and 1.1 resolves them to ``True``/``False``. Only
  ``true``/``false`` are booleans here.
- **Duplicate keys.** 1.1 lets the last one win silently, which quietly
  discards a declaration the file plainly contains.

Two more 1.1 coercions survive on purpose — the implicit timestamp
(``2024-01-01`` → ``date``) and sexagesimal ints (``12:30`` → ``750``). Both
interact with the unimplemented ``dtype: datetime``, so they belong to the
dtype guard in #65 rather than to the loader.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The YAML 1.2 core-schema boolean set — nothing else resolves to a bool.
_BOOL_1_2 = re.compile(r'^(?:true|True|TRUE|false|False|FALSE)$')

_Path = tuple[str | int, ...]


class _MarkedLoader(yaml.SafeLoader):
    """SafeLoader with 1.2 booleans. Marks are read off the node tree, not here."""


def _install_bool_resolver(loader: type[yaml.SafeLoader]) -> None:
    # Copy before mutating: the resolver table is inherited from SafeLoader, so
    # editing in place would reconfigure PyYAML for the whole process.
    loader.yaml_implicit_resolvers = {
        ch: [(tag, rx) for tag, rx in pairs if tag != 'tag:yaml.org,2002:bool']
        for ch, pairs in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    loader.add_implicit_resolver('tag:yaml.org,2002:bool', _BOOL_1_2, list('tTfF'))


_install_bool_resolver(_MarkedLoader)


class SourceMap:
    """Maps a path into the document (``variables`` → ``p`` → ``where``) to a line.

    Positions are best-effort by design: a path that does not resolve — an
    absent optional key, a pydantic ``loc`` naming a union member rather than a
    document node — degrades to the nearest ancestor that does, and finally to
    the file name alone. An error that cannot be placed is still an error worth
    reporting.
    """

    def __init__(self, marks: dict[_Path, tuple[int, int]] | None = None, path: Path | str | None = None) -> None:
        self._marks: dict[_Path, tuple[int, int]] = marks or {}
        self._path = str(path) if path is not None else None

    @classmethod
    def none(cls) -> SourceMap:
        """A map for input that never had a file — a dict passed to ``build()``."""
        return cls()

    def line(self, *keys: str | int) -> int | None:
        """1-based line of the deepest resolvable prefix of *keys*, if any."""
        found = None
        for i in range(len(keys) + 1):
            mark = self._marks.get(tuple(keys[:i]))
            if mark is not None:
                found = mark[0]
        return found

    def at(self, *keys: str | int) -> str:
        """``'model.yaml:12'`` — or ``''`` when there is nothing to point at."""
        if self._path is None:
            return ''
        line = self.line(*keys)
        return f'{self._path}:{line}' if line is not None else self._path

    def where(self, *keys: str | int) -> str:
        """``' (model.yaml:12)'``, ready to append to a message context."""
        located = self.at(*keys)
        return f' ({located})' if located else ''


def _collect(node: yaml.Node, path: _Path, marks: dict[_Path, tuple[int, int]], where: str) -> None:
    """Record 1-based (line, col) for every key and item under *node*."""
    if isinstance(node, yaml.MappingNode):
        seen: dict[Any, int] = {}
        for key_node, value_node in node.value:
            key = key_node.value
            if key in seen:
                msg = (
                    f'{where}:{key_node.start_mark.line + 1}: duplicate key {key!r} — '
                    f'first declared on line {seen[key]}. YAML would silently keep the '
                    f'last one, discarding a declaration the file contains.'
                )
                raise ValueError(msg)
            seen[key] = key_node.start_mark.line + 1
            child = (*path, key)
            marks[child] = (key_node.start_mark.line + 1, key_node.start_mark.column + 1)
            _collect(value_node, child, marks, where)
    elif isinstance(node, yaml.SequenceNode):
        for i, item in enumerate(node.value):
            child = (*path, i)
            marks[child] = (item.start_mark.line + 1, item.start_mark.column + 1)
            _collect(item, child, marks, where)


def read_yaml(path: Path | str) -> tuple[dict[str, Any], SourceMap]:
    """Load *path* and return its content plus a map from paths to lines."""
    text = Path(path).read_text()
    loader = _MarkedLoader(text)
    marks: dict[_Path, tuple[int, int]] = {}
    data: dict[str, Any] = {}
    try:
        node = loader.get_single_node()
        if node is not None:
            _collect(node, (), marks, str(path))
            data = loader.construct_document(node) or {}
    finally:
        loader.dispose()
    return data, SourceMap(marks, path)


def annotate(errors: Sequence[Any], source: SourceMap) -> str:
    """Render pydantic's ``ValidationError.errors()`` with source positions."""
    lines = []
    for err in errors:
        loc = err.get('loc', ())
        located = source.at(*loc)
        dotted = '.'.join(str(part) for part in loc)
        head = f'{located}: ' if located else ''
        body = f'{dotted}: {err.get("msg", "")}' if dotted else str(err.get('msg', ''))
        lines.append(head + body)
    return '\n'.join(lines)
