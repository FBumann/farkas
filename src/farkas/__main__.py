"""``python -m farkas <verb> ...`` — a shell front for the things that need no data.

Only ``latex`` today. Verbs that bind data belong in a caller's script, not
here: ``fk.solve`` takes a source mapping, and a CLI that tried to accept one
would be inventing a second way to spell it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from farkas.latex import to_latex


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m farkas')
    verbs = parser.add_subparsers(dest='verb', required=True)

    latex = verbs.add_parser('latex', help='render a model as LaTeX')
    latex.add_argument('model', help='path to a farkas YAML model')
    latex.add_argument('-o', '--out', help='write here instead of stdout')
    latex.add_argument('--symbols', help='sidecar YAML saying how names should print')
    latex.add_argument('--standalone', action='store_true', help='emit a compilable document')
    latex.add_argument('--no-legend', action='store_true', help='omit the sets/parameters/variables table')
    latex.add_argument('--no-numbers', action='store_true', help='use align* instead of align')

    args = parser.parse_args(argv)
    text = to_latex(
        args.model,
        symbols=args.symbols,
        standalone=args.standalone,
        legend=not args.no_legend,
        numbered=not args.no_numbers,
    )
    if args.out:
        Path(args.out).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
