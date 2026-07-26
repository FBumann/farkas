"""The ``lp_file`` sink: the model as LP text.

Portability, debugging, and the differential oracle. Every section is produced
by a duckdb ``COPY`` into a part file and the parts are concatenated bytewise,
so the LP text never exists in this process's memory either — only the file
handle does.

The one hand-managed chunk in the engine that is not label assignment lives
here: string aggregates do not spill, so the constraint text is emitted in
fixed row ranges. That costs nothing in a debugging sink.

Block order is not stable run to run (#109); the content is.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from linopy_yaml.relational.sinks.tables import ModelTables

#: Raw lines, no CSV quoting to undo.
_COPY_OPTS = "(FORMAT csv, HEADER false, QUOTE '', ESCAPE '')"


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

    con.execute(f"COPY (SELECT printf('%+.17g x%d', coeff, col) FROM obj) TO '{parts / 'obj'}' {_COPY_OPTS}")

    nnz = model.scalar('SELECT count(*) FROM A')
    avg = max(1, nnz // max(1, model.row_count))
    con_parts = []
    for i, (lo, hi) in enumerate(model.row_chunks(max(1, model.chunk_rows // avg))):
        part = parts / f'cons.{i}'
        con_parts.append(part)
        con.execute(
            f"""
            COPY (
                SELECT printf('c%d:', r.row) || chr(10)
                       || COALESCE(string_agg(printf('%+.17g x%d', a.coeff, a.col), chr(10)), '+0 x0')
                       || chr(10)
                       || printf('%s %.17g', CASE r.sense WHEN '==' THEN '=' ELSE r.sense END, r.rhs)
                FROM rows r LEFT JOIN A a USING (row)
                WHERE r.row >= {lo} AND r.row < {hi}
                GROUP BY r.row, r.sense, r.rhs
            ) TO '{part}' {_COPY_OPTS}
            """
        )

    con.execute(
        f"""
        COPY (
            SELECT CASE WHEN lb = '-infinity'::DOUBLE THEN '-infinity' ELSE printf('%.17g', lb) END
                   || printf(' <= x%d <= ', col)
                   || CASE WHEN ub = 'infinity'::DOUBLE THEN '+infinity' ELSE printf('%.17g', ub) END
            FROM cols
        ) TO '{parts / 'bounds'}' {_COPY_OPTS}
        """
    )

    integrality_sections = []
    for variable_type, keyword in (('binary', 'binary'), ('integer', 'general')):
        if model.scalar(f"SELECT count(*) FROM cols WHERE vtype = '{variable_type}'"):
            part = parts / keyword
            integrality_sections.append((keyword, part))
            con.execute(
                f"COPY (SELECT printf('x%d', col) FROM cols WHERE vtype = '{variable_type}') TO '{part}' {_COPY_OPTS}"
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
