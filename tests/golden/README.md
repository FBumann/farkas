# Golden output

One model, rendered in every format, committed and asserted byte for byte.

The same trade `examples/walkthrough.out` makes: reading the file is the same
as running the generator, and a format that starts saying something different
shows up as a diff here rather than as nothing at all.

Fragment assertions cannot do this job. They pin the constructs someone
thought to pin, and they survive anything that leaves those substrings intact
— a stray prefix, a lost space, a changed separator. Perturbing
`TypstFormat.summation` to emit `~sum_(...)` failed **no test** before these
files existed.

`model.yaml` is synthetic on purpose: it is the smallest model that reaches
every rendering path — each reduction, both translations, every bound shape,
both variable types, and a `where` with all three connectives. A real model
exercises a handful of those and reads better on a gallery page; this one is
here to be complete rather than to be read.

Regenerate after an intended change, then **read the diff** — that is the
review, and it is the whole point:

    uv run python -m tests.golden
