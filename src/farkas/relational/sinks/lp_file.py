"""The ``lp_file`` sink: the model as LP text.

Portability, debugging, and the differential oracle. Every section is sunk
straight into the open file, so the LP text never exists in this process's
memory — and no byte is written twice.

Numbers go through polars' float cast, which round-trips exactly: the text a
solver reads back is the double the engine computed.

**Every section is written in label order.** A solver does not care, but a
reader diffing two LP files does, and so does anyone checking that a model
builds the same bytes twice (#109).
"""

from __future__ import annotations

from pathlib import Path
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    import polars as pl

    from farkas.relational.sinks.tables import ModelTables


def _sink(frame: pl.LazyFrame, f: IO[bytes]) -> None:
    """Append a one-column frame to *f*, one raw line per row.

    A CSV writer with the CSV switched off: no header, no quoting, so the bytes
    on disk are exactly the strings the frame holds.

    The frame goes into the file the caller is already holding rather than into
    a part file to be concatenated afterwards. Sections are produced in the
    order the LP format wants them, so there is nothing to reorder — and a
    concatenation pass would read and rewrite the whole file, which at these
    sizes costs more than producing it did. polars writes through the handle's
    own buffer, so a ``f.write()`` between two sinks lands between them.
    """
    # `maintain_order` is polars' default and is what #109 rests on, so it is
    # stated rather than inherited: the parameter is documented as unstable,
    # and a default that flips would make the bytes non-reproducible silently.
    frame.sink_csv(f, include_header=False, quote_style='never', maintain_order=True)


def write_lp_file(model: ModelTables, path: str | Path) -> None:
    """Write the model as LP text."""
    import polars as pl

    path = Path(path)
    objective = model.obj.lazy().sort('col').select(_term(pl.col('coeff'), pl.col('col')))
    bounds = (
        model.cols.lazy()
        .sort('col')
        .select(
            pl.concat_str(
                _bound(pl.col('lb'), '-infinity').alias('lb'),
                pl.lit(' <= x').alias('open'),
                _digits(pl.col('col')),
                pl.lit(' <= ').alias('close'),
                _bound(pl.col('ub'), '+infinity').alias('ub'),
            )
        )
    )

    with open(path, 'wb') as f:
        f.write((b'min' if model.objective_sense == 'min' else b'max') + b'\n\nobj:\n')
        if model.objective_constant:
            f.write(f'{model.objective_constant:+.17g}\n'.encode())
        _sink(objective, f)

        f.write(b'\ns.t.\n\n')
        _sink(_constraint_blocks(model), f)

        f.write(b'\nbounds\n')
        _sink(bounds, f)

        for variable_type, keyword in (('binary', 'binary'), ('integer', 'general')):
            chosen = model.cols.lazy().filter(pl.col('vtype') == variable_type).sort('col')
            if chosen.select(pl.len()).collect().item() == 0:
                continue
            f.write(f'\n{keyword}\n'.encode())
            _sink(chosen.select(pl.concat_str(pl.lit('x'), _digits(pl.col('col')))), f)

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
        pl.concat_str(pl.lit('c').alias('c'), _digits(pl.col('row')), pl.lit(':').alias('colon')).alias('line'),
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
            _term(pl.col('coeff'), pl.col('col')).alias('line'),
        )
    )
    footer = rows.select(
        'row',
        pl.lit(_FOOTER, dtype=pl.Int64).alias('ord'),
        pl.concat_str(pl.col('sense').replace({'==': '='}), pl.lit(' '), _number(pl.col('rhs'))).alias('line'),
    )
    return pl.concat([header, placeholder, terms, footer]).sort('row', 'ord').select('line')


def _term(coeff: pl.Expr, col: pl.Expr) -> pl.Expr:
    """One ``+1.5 x7`` term.

    Built by a single ``concat_str`` rather than by chaining ``+``. Every ``+``
    is its own pass allocating its own full-width string column, and a term has
    four pieces; this way the line is allocated once.
    """
    import polars as pl

    return pl.concat_str(*_signed(coeff), pl.lit(' x'), _digits(col))


def _number(value: pl.Expr) -> pl.Expr:
    """A float as LP text."""
    import polars as pl

    return value.cast(pl.String)


def _signed(value: pl.Expr) -> tuple[pl.Expr, pl.Expr]:
    """A coefficient, sign always explicit — the LP format needs the ``+``.

    Two pieces for ``concat_str`` rather than one finished string, because the
    cast already carries the ``-``: only a non-negative value needs a sign
    glued on, and the sign column is one character wide however large the
    model. Deciding the sign and then rendering ``abs()`` would render the
    magnitude in both arms of the ``when``, at full width, to discard one.

    ``-0.0`` is why zero is spelled out rather than cast: it is ``>= 0``, so it
    takes the ``+`` arm while the cast still renders ``-0.0``, giving
    ``+-0.0``, which no LP parser accepts. It is reachable from any negative
    coefficient times a zero parameter, so it is a real file, not a curiosity.
    """
    import polars as pl

    return (
        pl.when(value >= 0).then(pl.lit('+')).otherwise(pl.lit('')).alias('sign'),
        pl.when(value == 0).then(pl.lit('0.0')).otherwise(_number(value)).alias('magnitude'),
    )


def _bound(value: pl.Expr, infinite: str) -> pl.Expr:
    """A bound, with the LP format's own spelling for an unbounded one."""
    import polars as pl

    return pl.when(value.is_infinite()).then(pl.lit(infinite)).otherwise(_number(value))


def _digits(value: pl.Expr) -> pl.Expr:
    """An index as text — never in scientific notation, whatever its size."""
    import polars as pl

    return value.cast(pl.Int64).cast(pl.String)
