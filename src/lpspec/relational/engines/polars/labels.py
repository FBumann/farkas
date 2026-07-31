"""Dense solver indices for a masked coordinate product.

**Labels are the one place order is load-bearing.** ``var_label`` *is* the
solver's column index and ``row`` its row index, so a label is not a detail of
how the executor happens to number things — it is the model's identity, and two
builds of one model must agree on it integer for integer (docs/ARCHITECTURE.md,
"The relational lane").

Variables and constraint rows are the same operation over different frames, so
:meth:`Labeller.frame` is written once; twice is how the two would come to
disagree about which coordinate gets which index. It reaches an answer three
ways depending on how much of the coordinate product survives the mask —
arithmetic, factored, counted — and *those* must agree with each other, which
is what makes this a module rather than three methods among twenty.

**Which** of the three is a question about the plan, not about polars, so it is
asked of `plan.free_prefix` and answered identically for the duckdb engine. Only
the three executions live here.

Its inputs are stated rather than reached for: a labeller needs the query
(to build the masked product), the dimension cardinalities (to do the
arithmetic), and the program (to know which dims a mask reads). Nothing else
about the build can change a label.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import polars as pl

from lpspec.relational import plan
from lpspec.relational.engines.polars.compiler import UNIT, _ordinal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from lpspec.relational.engines.polars.compiler import PolarsCompiler


class Labeller:
    """Assigns dense ``0..n-1`` labels over a declaration's coordinate product."""

    def __init__(
        self,
        compiler: PolarsCompiler,
        dimension_cardinality: Mapping[str, int],
        program: plan.Program,
    ) -> None:
        self._q = compiler
        self._card = dimension_cardinality
        self._param_dims = plan.name_dims(program)

    def frame(
        self,
        dims: tuple[str, ...],
        where: plan.Predicate | None,
        label: str,
        start: int,
        restrictions: Sequence[tuple[tuple[str, ...], pl.LazyFrame]] = (),
    ) -> tuple[pl.DataFrame, int]:
        """The masked coord product of *dims* with a dense *label* from *start*.

        A label follows declaration order, which is what lets it *be* the
        solver's own index with no remapping.

        The mask chooses the path. **Unmasked**, every coordinate exists, so a
        row's label is its position in the product — arithmetic on the dim
        ordinals, with no sort and nothing to count. **Masked**, which rows
        survive is not known until the predicate has run, so the position has
        to be counted, and that costs a sort — unless the mask *factors*, which
        is :meth:`_factored`.

        All three return ``(dims…, label)`` in that column order. A mask that
        removes nothing has to be indistinguishable from no mask, down to the
        schema.

        *restrictions* are variable-presence frames a constraint row must be
        contained in: absence propagates into a comparison and drops the row
        (v1 ``convention.rst`` §6, §12). They are semi-joins, so they can only
        remove rows — but which rows is not known until data is read, and that
        is what costs the two fast paths, so the caller passes them only when a
        variable in the equation is actually masked.
        """
        if not restrictions:
            if where is None:
                frame = self._q.frame(dims, None)
                rows = math.prod(self._card[d] for d in dims)
                labelled = frame.select(*dims, self.row_major(dims, start).alias(label))
                return labelled.collect(engine='streaming'), start + rows

            free = plan.free_prefix(dims, plan.predicate_dims(where, self._param_dims))
            if free:
                return self._factored(dims, free, where, label, start)

        restricted = self._q.frame(dims, where)
        for on, presence in restrictions:
            restricted = restricted.join(presence.unique(), on=list(on), how='semi')

        materialised = (
            restricted.sort([_ordinal(d) for d in dims])
            # No dims means the carrier is `UNIT`: selecting nothing would drop
            # the one row this path exists to count.
            .select(*(dims or (UNIT,)))
            .with_row_index(label, offset=start)
            .select(*dims, pl.col(label).cast(pl.Int64))
            .collect(engine='streaming')
        )
        return materialised, start + materialised.height

    def _factored(
        self,
        dims: tuple[str, ...],
        free: int,
        where: plan.Predicate,
        label: str,
        start: int,
    ) -> tuple[pl.DataFrame, int]:
        """Labels for a mask that reads none of the first *free* dims.

        A mask that cannot see the leading dims removes the same coordinates
        under every one of their values, so the survivors are a rectangle: the
        full product of the leading dims against one surviving set. Ranking
        that set costs a sort of the *set*, not of the product — on `dispatch`
        it is a sort of the generators rather than of 10M
        ``(snapshot, generator)`` pairs.

        The label is then arithmetic again, through the same
        :meth:`row_major` the unmasked path uses: row-major over the leading
        dims, times the width of the surviving set, plus a survivor's rank
        within it. That is the same number the sort would have counted, because
        for each leading coordinate the same survivors appear in the same order.

        The prefix has to be *leading* rather than merely absent from the mask:
        a label follows declaration order, and only a prefix leaves the
        surviving set contiguous within it.
        """

        head, kept = dims[:free], dims[free:]
        survivors = (
            self._q.frame(kept, where)
            .sort([_ordinal(d) for d in kept])
            .select(*kept)
            .with_row_index('__rank')
            .collect(engine='streaming')
        )
        width = survivors.height
        if width == 0:
            # nothing survived anywhere, so there is no rectangle to describe.
            # The counted path returns the right columns and dtypes for free.
            empty = (
                self._q.frame(dims, where)
                .select(*dims)
                .with_row_index(label, offset=start)
                .select(*dims, pl.col(label).cast(pl.Int64))
                .collect(engine='streaming')
            )
            return empty, start

        labelled = (
            self._q.frame(head, None)
            .select(*head, self.row_major(head, 0).alias('__position'))
            .join(survivors.lazy(), how='cross')
            .select(
                *dims,
                (pl.lit(start, dtype=pl.Int64) + pl.col('__position') * width + pl.col('__rank')).alias(label),
            )
            .collect(engine='streaming')
        )
        return labelled, start + labelled.height

    def row_major(self, dims: tuple[str, ...], start: int) -> pl.Expr:
        """Row-major position in *dims*' coordinate product, offset by *start*.

        The trailing dim has stride 1 and every other is the product of the
        cardinalities to its right, so the position is a dot product against the
        ordinals the frame already carries — no ordering imposed, because the
        answer does not depend on the order rows arrive in.

        Both arithmetic paths of :meth:`frame` reach a label through this,
        written once for the reason the label itself is: two copies would come
        to disagree about which coordinate gets which solver index.
        """

        stride, position = 1, pl.lit(start, dtype=pl.Int64)
        for d in reversed(dims):
            position = position + pl.col(_ordinal(d)) * stride
            stride *= self._card[d]
        return position
