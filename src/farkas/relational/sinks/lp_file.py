"""The ``lp_file`` sink: the model as LP text.

Portability, debugging, and the differential oracle. Every section is produced
by a duckdb ``COPY`` into a part file and the parts are concatenated bytewise,
so the LP text never exists in this process's memory either — only the file
handle does.

The one hand-managed chunk in the engine that is not label assignment lives
here: string aggregates do not spill, so the constraint text is emitted in
fixed row ranges. That costs nothing in a debugging sink.

Numbers are rendered with ``::VARCHAR`` rather than ``printf('%.17g')``.
duckdb's double-to-text cast is shortest-round-trip, so it is exact — and it
is 30-40% faster than ``printf`` on every section, which is most of what emit
costs (the relational work in this file is under 0.1s of a 2s emit at 10M
columns; the rest is float-to-text and the write). See
:func:`_signed` for the one trap that costs.

Block order is not stable run to run (#109); the content is.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from farkas.relational.sql import path_literal

if TYPE_CHECKING:
    from farkas.relational.sinks.tables import ModelTables

#: Raw lines, no CSV quoting to undo.
_COPY_OPTS = "(FORMAT csv, HEADER false, QUOTE '', ESCAPE '')"


def _signed(coeff: str) -> str:
    """A coefficient with the explicit sign LP terms carry.

    ``+ 0.0`` is not decoration. ``-0.0`` is reachable — any negative
    coefficient times a zero parameter — and it satisfies ``>= 0``, so the sign
    arm fires while the cast still renders ``-0.0``, giving ``+-0.0``. Adding
    zero normalises it away for free. The obvious alternative,
    ``replace('+' || cast, '+-', '-')``, costs the whole speed win: the extra
    string pass is as expensive as the ``printf`` it replaces.
    """
    return f"CASE WHEN {coeff} >= 0 THEN '+' ELSE '' END || ({coeff} + 0.0)::VARCHAR"


def write_lp_file(model: ModelTables, path: str | Path) -> None:
    """Write the model as LP text.

    Every section is produced by a duckdb ``COPY`` into a part file, then the
    parts are concatenated bytewise — so the LP text never exists in this
    process's memory either, only the file handle does.
    """
    path = Path(path)
    parts = model.workdir / 'lp_parts'
    parts.mkdir(exist_ok=True)
    con = model.connection

    con.execute(
        f"COPY (SELECT {_signed('coeff')} || ' x' || col::VARCHAR FROM obj) "
        f'TO {path_literal(parts / "obj")} {_COPY_OPTS}'
    )

    con_parts = []
    for i, (lo, hi) in enumerate(model.row_chunks_by_nonzeros(model.chunk_rows)):
        part = parts / f'cons.{i}'
        con_parts.append(part)
        con.execute(
            f"""
            COPY (
                SELECT 'c' || r.row::VARCHAR || ':' || chr(10)
                       || COALESCE(string_agg({_signed('a.coeff')} || ' x' || a.col::VARCHAR, chr(10)), '+0 x0')
                       || chr(10)
                       || (CASE r.sense WHEN '==' THEN '=' ELSE r.sense END) || ' ' || r.rhs::VARCHAR
                FROM rows r LEFT JOIN A a USING (row)
                WHERE r.row >= {lo} AND r.row < {hi}
                GROUP BY r.row, r.sense, r.rhs
            ) TO {path_literal(part)} {_COPY_OPTS}
            """
        )

    con.execute(
        f"""
        COPY (
            SELECT CASE WHEN lb = '-infinity'::DOUBLE THEN '-infinity' ELSE lb::VARCHAR END
                   || ' <= x' || col::VARCHAR || ' <= '
                   || CASE WHEN ub = 'infinity'::DOUBLE THEN '+infinity' ELSE ub::VARCHAR END
            FROM cols
        ) TO {path_literal(parts / 'bounds')} {_COPY_OPTS}
        """
    )

    integrality_sections = []
    for variable_type, keyword in (('binary', 'binary'), ('integer', 'general')):
        if model.scalar(f"SELECT count(*) FROM cols WHERE vtype = '{variable_type}'"):
            part = parts / keyword
            integrality_sections.append((keyword, part))
            con.execute(
                f"COPY (SELECT 'x' || col::VARCHAR FROM cols WHERE vtype = '{variable_type}') "
                f'TO {path_literal(part)} {_COPY_OPTS}'
            )

    sense = b'min' if model.objective_sense == 'min' else b'max'
    with open(path, 'wb') as f:
        f.write(sense + b'\n\nobj:\n')
        if model.objective_constant:
            f.write(f'{model.objective_constant:+.17g}\n'.encode())
        _cat(f, parts / 'obj')
        f.write(b'\ns.t.\n\n')
        for part in con_parts:
            _cat(f, part)
        f.write(b'\nbounds\n')
        _cat(f, parts / 'bounds')
        for keyword, part in integrality_sections:
            f.write(f'\n{keyword}\n'.encode())
            _cat(f, part)
        f.write(b'\nend\n')
    shutil.rmtree(parts)


def _cat(f: Any, part: Path) -> None:
    with open(part, 'rb') as src:
        shutil.copyfileobj(src, f)
