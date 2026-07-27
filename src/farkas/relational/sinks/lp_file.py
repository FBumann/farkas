"""The ``lp_file`` sink: the model as LP text.

Portability, debugging, and the differential oracle. Every section is sunk
straight to a part file and the parts concatenated bytewise, so the LP text
never exists in this process's memory.

Numbers go through polars' float cast, which round-trips exactly: the text a
solver reads back is the double the engine computed.

**Every section is written in label order.** A solver does not care, but a
reader diffing two LP files does, and so does anyone checking that a model
builds the same bytes twice (#109).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import polars as pl

    from farkas.relational.sinks.tables import ModelTables


def _sink(frame: pl.LazyFrame, part: Path) -> None:
    """Write a one-column frame to *part*, one raw line per row.

    A CSV writer with the CSV switched off: no header, no quoting, so the
    bytes on disk are exactly the strings the frame holds.
    """
    frame.sink_csv(part, include_header=False, quote_style='never')


def write_lp_file(model: ModelTables, path: str | Path) -> None:
    """Write the model as LP text."""
    import polars as pl

    path = Path(path)
    with tempfile.TemporaryDirectory(prefix='farkas-lp-') as tmp:
        parts = Path(tmp)

        objective = (
            model.obj.lazy().sort('col').select(_signed(pl.col('coeff')) + pl.lit(' x') + _digits(pl.col('col')))
        )
        _sink(objective, parts / 'obj')

        _sink(_constraint_blocks(model), parts / 'cons')

        bounds = (
            model.cols.lazy()
            .sort('col')
            .select(
                _bound(pl.col('lb'), '-infinity')
                + pl.lit(' <= x')
                + _digits(pl.col('col'))
                + pl.lit(' <= ')
                + _bound(pl.col('ub'), '+infinity')
            )
        )
        _sink(bounds, parts / 'bounds')

        integrality = []
        for variable_type, keyword in (('binary', 'binary'), ('integer', 'general')):
            chosen = model.cols.lazy().filter(pl.col('vtype') == variable_type).sort('col')
            if chosen.select(pl.len()).collect().item() == 0:
                continue
            part = parts / keyword
            integrality.append((keyword, part))
            _sink(chosen.select(pl.lit('x') + _digits(pl.col('col'))), part)

        sense = b'min' if model.objective_sense == 'min' else b'max'
        with open(path, 'wb') as f:
            f.write(sense + b'\n\nobj:\n')
            if model.objective_constant:
                f.write(f'{model.objective_constant:+.17g}\n'.encode())
            _cat(f, parts / 'obj')
            f.write(b'\ns.t.\n\n')
            _cat(f, parts / 'cons')
            f.write(b'\nbounds\n')
            _cat(f, parts / 'bounds')
            for keyword, part in integrality:
                f.write(f'\n{keyword}\n'.encode())
                _cat(f, part)
            f.write(b'\nend\n')


#: Sort keys placing a row's header before its terms and its sense after them.
#: A term sorts on its own column index, which is why the footer has to outrank
#: every column a model could have.
_HEADER, _PLACEHOLDER, _FOOTER = -2, -1, 2**62


def _constraint_blocks(model: ModelTables) -> pl.LazyFrame:
    """Every constraint line, as one sorted stream of ``(row, ord, line)``.

    One line per output line rather than one block per row: the pieces are
    built independently and interleaved by sorting, so nothing has to gather a
    row's terms into a string first. That is what makes the bytes reproducible
    — a hash join hands back groups in whatever order it finishes them, and no
    amount of sorting the *rows* afterwards fixes the order *within* one.

    A row with no terms still needs a line a solver can parse, and the anti-join
    is what a group-by gave for free.
    """
    import polars as pl

    rows = model.rows.lazy()
    header = rows.select(
        'row',
        pl.lit(_HEADER, dtype=pl.Int64).alias('ord'),
        (pl.lit('c') + _digits(pl.col('row')) + pl.lit(':')).alias('line'),
    )
    placeholder = rows.join(model.matrix.lazy().select('row').unique(), on='row', how='anti').select(
        'row',
        pl.lit(_PLACEHOLDER, dtype=pl.Int64).alias('ord'),
        pl.lit('+0 x0').alias('line'),
    )
    terms = (
        model.matrix.lazy()
        .sort('row', 'col')
        .select(
            'row',
            pl.col('col').cast(pl.Int64).alias('ord'),
            (_signed(pl.col('coeff')) + pl.lit(' x') + _digits(pl.col('col'))).alias('line'),
        )
    )
    footer = rows.select(
        'row',
        pl.lit(_FOOTER, dtype=pl.Int64).alias('ord'),
        (pl.col('sense').replace({'==': '='}) + pl.lit(' ') + _number(pl.col('rhs'))).alias('line'),
    )
    return pl.concat([header, placeholder, terms, footer]).sort('row', 'ord').select('line')


def _number(value: pl.Expr) -> pl.Expr:
    """A float as LP text."""
    import polars as pl

    return value.cast(pl.String)


def _signed(value: pl.Expr) -> pl.Expr:
    """A coefficient, sign always explicit — the LP format needs the ``+``.

    The sign is decided and *then* the magnitude is rendered, rather than a
    ``+`` being prefixed to whatever the cast produced. ``-0.0`` is why: it
    satisfies ``>= 0``, so prefixing gives ``+-0.0``, which no LP parser
    accepts. It is reachable from any negative coefficient times a zero
    parameter, so it is a real file, not a curiosity.
    """
    import polars as pl

    magnitude = _number(value.abs())
    return pl.when(value < 0).then(pl.lit('-') + magnitude).otherwise(pl.lit('+') + magnitude)


def _bound(value: pl.Expr, infinite: str) -> pl.Expr:
    """A bound, with the LP format's own spelling for an unbounded one."""
    import polars as pl

    return pl.when(value.is_infinite()).then(pl.lit(infinite)).otherwise(_number(value))


def _digits(value: pl.Expr) -> pl.Expr:
    """An index as text — never in scientific notation, whatever its size."""
    import polars as pl

    return value.cast(pl.Int64).cast(pl.String)


def _cat(f: Any, part: Path) -> None:
    with open(part, 'rb') as src:
        shutil.copyfileobj(src, f)
