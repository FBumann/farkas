"""How this project reads a YAML file.

`yaml.safe_load` implements YAML 1.1, and two of its rules are actively wrong
for a language whose scalars are user data. The loader is the only layer that
can see them, so both are fixed here:

- **1.2 booleans.** ``on``/``off``/``yes``/``no``/``y``/``n`` are ordinary
  dimension labels — country codes, region names, tech names. YAML 1.1
  resolves them to ``True``/``False``, and the rows they keyed then vanish
  from the model without a word. Only ``true``/``false`` are booleans here,
  which is the YAML 1.2 core schema.
- **Duplicate keys.** 1.1 lets the last one win silently, discarding a
  declaration the file plainly contains.

Two further 1.1 coercions survive on purpose — the implicit timestamp
(``2024-01-01`` → ``date``) and sexagesimal ints (``12:30`` → ``750``). Both
interact with the ``dtype: datetime`` the schema accepts and does not yet
implement, so they belong to the dtype guard in #65 rather than here.

The output is plain ``dict``/``str``: no loader wrapper reaches the schema,
the AST, the IR, or duckdb.

Reading the nodes is also the only chance to learn **where** each declaration
sits. ``yaml.safe_load`` discards positions the moment a document parses, so
every later error — a schema-shape complaint, an unparseable expression, an
unresolved name — can name the declaration but not the line. The node walk
that checks for duplicate keys therefore also records each key's line into a
side table, and :class:`SourceMap` turns a path into the document into a
``file:line`` prefix.
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


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader with 1.2 booleans. Duplicate keys are checked on the nodes."""


def _install_bool_resolver(loader: type[yaml.SafeLoader]) -> None:
    # Copy before mutating: the resolver table is inherited from SafeLoader, so
    # editing it in place would reconfigure PyYAML for the whole process.
    loader.yaml_implicit_resolvers = {
        ch: [(tag, rx) for tag, rx in pairs if tag != 'tag:yaml.org,2002:bool']
        for ch, pairs in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    loader.add_implicit_resolver('tag:yaml.org,2002:bool', _BOOL_1_2, list('tTfF'))


_install_bool_resolver(_StrictLoader)


_Path = tuple[str | int, ...]


class SourceMap:
    """Maps a path into the document (``variables`` → ``p`` → ``where``) to a line.

    Positions are best-effort by design: a path that does not resolve — an
    absent optional key, a pydantic ``loc`` naming a union member rather than a
    document node — degrades to the nearest ancestor that does, and finally to
    the file name alone. An error that cannot be placed is still an error worth
    reporting.
    """

    def __init__(self, marks: dict[_Path, int] | None = None, path: Path | str | None = None) -> None:
        self._marks: dict[_Path, int] = marks or {}
        self._path = str(path) if path is not None else None

    @classmethod
    def none(cls) -> SourceMap:
        """A map for input that never had a file — a dict passed to ``build()``."""
        return cls()

    def line(self, *keys: str | int) -> int | None:
        """1-based line of the deepest resolvable prefix of *keys*, if any."""
        for i in range(len(keys), -1, -1):
            line = self._marks.get(tuple(keys[:i]))
            if line is not None:
                return line
        return None

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


def _walk(node: yaml.Node, path: _Path, marks: dict[_Path, int], where: str) -> None:
    """Record each key's line, and reject a mapping that declares one twice.

    Duplicates are checked on the node tree before construction, so a ``<<:``
    merge key that a mapping overrides is not a duplicate — the override is
    the point.
    """
    if isinstance(node, yaml.MappingNode):
        seen: dict[Any, int] = {}
        for key_node, value_node in node.value:
            key = key_node.value
            line = key_node.start_mark.line + 1
            if key in seen:
                msg = (
                    f'{where}:{line}: duplicate key {key!r} — first declared on '
                    f'line {seen[key]}. YAML would silently keep the last one, '
                    f'discarding a declaration the file contains.'
                )
                raise ValueError(msg)
            seen[key] = line
            child = (*path, key)
            marks[child] = line
            _walk(value_node, child, marks, where)
    elif isinstance(node, yaml.SequenceNode):
        for i, item in enumerate(node.value):
            child = (*path, i)
            marks[child] = item.start_mark.line + 1
            _walk(item, child, marks, where)


def read_yaml(path: Path | str) -> tuple[dict[str, Any], SourceMap]:
    """Load *path* as a mapping of sections, plus a map from paths to lines."""
    where = str(path)
    loader = _StrictLoader(Path(path).read_text())
    marks: dict[_Path, int] = {}
    try:
        node = loader.get_single_node()
        if node is None:
            return {}, SourceMap(marks, path)
        _walk(node, (), marks, where)
        data = loader.construct_document(node)
    finally:
        loader.dispose()
    if data is None:
        return {}, SourceMap(marks, path)
    if not isinstance(data, dict):
        msg = f'{where}: a model file must be a mapping of sections (dimensions:, variables:, …), got {type(data).__name__}.'
        raise ValueError(msg)
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
