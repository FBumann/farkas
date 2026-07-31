"""SVG charts for docs/benchmarks.md, written by hand and by rule.

No plotting library: the pages this feeds are read on GitHub as often as on
the site, so a chart has to be a committed file rather than something a
renderer produces at view time — and a dependency that draws axes is a poor
trade for a build that has to stay installable in seconds.

**Two files per chart, light and dark.** mkdocs-material's palette toggle
stamps a scheme on the *host* page, which an ``<img>``-referenced SVG cannot
see, so the page carries both and Material's ``#only-light`` / ``#only-dark``
suffixes choose. Dark is the site's default scheme, so it is drawn first and
is not an inverted afterthought.

**The palette is validated, not chosen.** Slots 1 and 2 of the reference
categorical palette, whose steps clear the lightness band, the chroma floor,
CVD separation, the normal-vision floor and contrast against both surfaces.
Series identity is carried twice — colour *and* a direct label at the end of
each line — so it survives a reader who sees neither hue.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


@dataclass(frozen=True)
class Scheme:
    """One mode's surface and ink. Roles, not raw hex, at every use site."""

    name: str
    surface: str
    text: str
    muted: str
    grid: str
    series: tuple[str, str]


#: Dark first: it is the site's default scheme (`mkdocs.yml`, slate before
#: default), so it is the one most readers see. Both are validated against
#: their own surface — `node scripts/validate_palette.js` in the dataviz
#: skill, all six checks, both modes.
DARK = Scheme('dark', '#1e2129', '#e6e8ea', '#9aa3ad', '#2f343e', ('#3987e5', '#d95926'))
LIGHT = Scheme('light', '#ffffff', '#1a1c1f', '#5c646d', '#e6e9ec', ('#2a78d6', '#eb6834'))
SCHEMES = (LIGHT, DARK)

FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
MONO = "ui-monospace, 'JetBrains Mono', 'SF Mono', Menlo, monospace"


@dataclass
class Series:
    """One line: a name, a colour slot, and points in data space."""

    label: str
    points: list[tuple[float, float]]
    slot: int = 0
    dashed: bool = False


@dataclass
class Panel:
    """One facet — its own title, its own y range, a shared x scale."""

    title: str
    series: list[Series]
    note: str = ''


@dataclass
class Figure:
    """A row of panels sharing an x axis, a y unit and a legend."""

    panels: list[Panel]
    x_label: str
    y_label: str
    y_unit: str = 's'
    width: int = 860
    panel_height: int = 230
    log_x: bool = True
    log_y: bool = True
    caption: str = ''
    provenance: str = ''
    _extra: list[str] = field(default_factory=list)


def _esc(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _si(value: float) -> str:
    """A tick label a reader can say out loud: 10k, 1M, 120M."""
    for cut, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'k')):
        if value >= cut:
            scaled = value / cut
            return f'{scaled:.0f}{suffix}' if scaled >= 10 or scaled == int(scaled) else f'{scaled:.1f}{suffix}'
    return f'{value:.0f}'


def _seconds(value: float) -> str:
    if value >= 10:
        return f'{value:.0f}s'
    if value >= 1:
        return f'{value:.1f}s'
    if value >= 0.01:
        return f'{value:.2f}s'
    return f'{value * 1000:.0f}ms'


def _decades(lo: float, hi: float) -> list[float]:
    """Powers of ten spanning the data, so a log axis is read, not decoded."""
    start = math.floor(math.log10(max(lo, 1e-12)))
    end = math.ceil(math.log10(max(hi, 1e-11)))
    return [10.0**e for e in range(int(start), int(end) + 1)]


def render(figure: Figure, scheme: Scheme) -> str:
    """*figure* as a standalone SVG document for *scheme*."""
    n = len(figure.panels)
    pad_l, pad_r, pad_t, pad_b = 58, 104, 62, 46
    gap = 34
    inner_w = (figure.width - pad_l - pad_r - gap * (n - 1)) / n
    inner_h = figure.panel_height
    height = pad_t + inner_h + pad_b + (28 if figure.caption else 0)

    xs = [p[0] for panel in figure.panels for s in panel.series for p in s.points]
    ys = [p[1] for panel in figure.panels for s in panel.series for p in s.points]
    x0, x1 = min(xs), max(xs)
    y_ticks = _decades(min(ys), max(ys)) if figure.log_y else _linear_ticks(min(ys), max(ys))
    y0, y1 = y_ticks[0], y_ticks[-1]

    def sx(value: float, offset: float) -> float:
        span = math.log10(x1) - math.log10(x0) if figure.log_x else x1 - x0
        pos = (math.log10(value) - math.log10(x0)) if figure.log_x else (value - x0)
        return offset + inner_w * (pos / (span or 1))

    def sy(value: float) -> float:
        if figure.log_y:
            span = math.log10(y1) - math.log10(y0)
            pos = math.log10(max(value, y0)) - math.log10(y0)
        else:
            span, pos = (y1 - y0), (value - y0)
        return pad_t + inner_h * (1 - pos / (span or 1))

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {figure.width} {height}" '
        f'width="{figure.width}" height="{height}" font-family="{FONT}" '
        f'role="img" aria-label="{_esc(figure.caption or figure.y_label)}">',
        f'<title>{_esc(figure.caption or figure.y_label)}</title>',
        f'<rect width="{figure.width}" height="{height}" fill="{scheme.surface}"/>',
    ]
    if figure.provenance:
        out.append(f'<!-- {_esc(figure.provenance)} -->')

    for index, panel in enumerate(figure.panels):
        offset = pad_l + index * (inner_w + gap)
        out.append(
            f'<text x="{offset:.0f}" y="{pad_t - 16:.0f}" font-size="13" font-weight="600" '
            f'fill="{scheme.text}">{_esc(panel.title)}</text>'
        )
        if panel.note:
            out.append(
                f'<text x="{offset:.0f}" y="{pad_t - 6:.0f}" font-size="11" '
                f'fill="{scheme.muted}">{_esc(panel.note)}</text>'
            )
        # gridlines first, and recessive: they orient, they do not compete
        for tick in y_ticks:
            y = sy(tick)
            out.append(
                f'<line x1="{offset:.0f}" y1="{y:.1f}" x2="{offset + inner_w:.0f}" y2="{y:.1f}" '
                f'stroke="{scheme.grid}" stroke-width="1"/>'
            )
            if index == 0:
                label = _seconds(tick) if figure.y_unit == 's' else _si(tick) if tick >= 1 else f'{tick:g}'
                out.append(
                    f'<text x="{offset - 8:.0f}" y="{y + 4:.1f}" font-size="11" text-anchor="end" '
                    f'font-family="{MONO}" fill="{scheme.muted}">{label}</text>'
                )
        decades = [d for d in _decades(x0, x1) if x0 <= d <= x1]
        # a six-panel figure has no room for every decade; drop alternates
        # rather than let the labels touch, keeping the first and the last
        stride = 1 if inner_w / max(len(decades), 1) > 42 else 2
        for di, tick in enumerate(decades):
            if di % stride and di != len(decades) - 1:
                continue
            x = sx(tick, offset)
            out.append(
                f'<text x="{x:.1f}" y="{pad_t + inner_h + 18:.0f}" font-size="11" text-anchor="middle" '
                f'font-family="{MONO}" fill="{scheme.muted}">{_si(tick)}</text>'
            )

        for s in panel.series:
            colour = scheme.series[s.slot]
            path = ' '.join(
                f'{"M" if i == 0 else "L"}{sx(px, offset):.1f},{sy(py):.1f}' for i, (px, py) in enumerate(s.points)
            )
            dash = ' stroke-dasharray="5 3"' if s.dashed else ''
            out.append(
                f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="2" '
                f'stroke-linejoin="round" stroke-linecap="round"{dash}/>'
            )
            for px, py in s.points:
                # a 2px surface ring keeps markers legible where lines cross
                out.append(
                    f'<circle cx="{sx(px, offset):.1f}" cy="{sy(py):.1f}" r="3.4" fill="{colour}" '
                    f'stroke="{scheme.surface}" stroke-width="2"/>'
                )
        if index == n - 1:  # direct labels, so identity is never colour alone
            placed: list[float] = []
            for s in sorted(panel.series, key=lambda s: sy(s.points[-1][1])):
                y = sy(s.points[-1][1]) + 4
                # nudge apart where two lines converge, rather than overprint
                while any(abs(y - taken) < 14 for taken in placed):
                    y += 14
                placed.append(y)
                out.append(
                    f'<text x="{sx(s.points[-1][0], offset) + 12:.1f}" y="{y:.1f}" font-size="12" '
                    f'font-weight="600" fill="{scheme.series[s.slot]}">{_esc(s.label)}</text>'
                )

    out.append(
        f'<text x="{pad_l}" y="{height - (34 if figure.caption else 12):.0f}" font-size="11" '
        f'fill="{scheme.muted}">{_esc(figure.x_label)}</text>'
    )
    out.append(
        f'<text x="{pad_l - 6}" y="{pad_t - 38:.0f}" font-size="11" letter-spacing="0.04em" '
        f'fill="{scheme.muted}">{_esc(figure.y_label.upper())}</text>'
    )
    if figure.caption:
        out.append(
            f'<text x="{pad_l}" y="{height - 12:.0f}" font-size="11.5" '
            f'fill="{scheme.muted}">{_esc(figure.caption)}</text>'
        )
    out += figure._extra
    out.append('</svg>')
    return '\n'.join(out)


def _linear_ticks(lo: float, hi: float) -> list[float]:
    span = hi - lo or 1
    step = 10 ** math.floor(math.log10(span))
    step = next(step * m for m in (1, 2, 2.5, 5, 10) if span / (step * m) <= 5)
    start = math.floor(lo / step) * step
    ticks = []
    while start <= hi + step * 0.5:
        ticks.append(round(start, 10))
        start += step
    return ticks


def bars(
    groups: Sequence[tuple[str, Sequence[tuple[str, float]]]],
    phases: Sequence[str],
    scheme: Scheme,
    *,
    width: int = 860,
    caption: str = '',
    provenance: str = '',
) -> str:
    """Stacked bars: one bar per arm, segments per phase.

    Where the line charts answer *how much*, this answers *where* — and it is
    the chart the tables cannot be read for at all, since they publish one
    total per row.
    """
    pad_l, pad_r, pad_t, pad_b = 124, 132, 34, 56
    row_h, row_gap, group_gap, head_h = 24, 8, 20, 22
    rows = sum(len(g[1]) for g in groups)
    height = pad_t + rows * (row_h + row_gap) + len(groups) * (group_gap + head_h) + pad_b
    inner_w = width - pad_l - pad_r
    longest = max(sum(value for _, value in values) for _, arms in groups for _, values in arms)

    # phase shades: one hue, light to dark, because phases are parts of a
    # magnitude rather than identities that could be confused for each other
    shades = (
        ('#7cb8f5', '#3987e5', '#1b4f8f', '#0d2c52')
        if scheme.name == 'dark'
        else ('#9cc6f2', '#4a90e2', '#1d5fae', '#0f3a70')
    )

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" font-family="{FONT}" role="img" aria-label="{_esc(caption)}">',
        f'<title>{_esc(caption)}</title>',
        f'<rect width="{width}" height="{height}" fill="{scheme.surface}"/>',
    ]
    if provenance:
        out.append(f'<!-- {_esc(provenance)} -->')

    y = pad_t
    for gi, (group, arms) in enumerate(groups):
        if gi:
            y += group_gap
        out.append(
            f'<text x="14" y="{y + 12:.0f}" font-size="12.5" font-weight="600" '
            f'fill="{scheme.text}">{_esc(group)}</text>'
        )
        y += head_h
        for arm, values in arms:
            total = sum(v for _, v in values)
            x = pad_l
            for pi, (_phase, value) in enumerate(values):
                w = inner_w * value / longest
                if w > 0.4:
                    out.append(
                        f'<rect x="{x:.1f}" y="{y:.0f}" width="{max(w - 2, 0.6):.1f}" height="{row_h}" '
                        f'rx="3" fill="{shades[pi % len(shades)]}"/>'
                    )
                x += w
            out.append(
                f'<text x="{pad_l - 10:.0f}" y="{y + row_h * 0.68:.0f}" font-size="11.5" text-anchor="end" '
                f'font-family="{MONO}" fill="{scheme.muted}">{_esc(arm)}</text>'
            )
            out.append(
                f'<text x="{x + 10:.1f}" y="{y + row_h * 0.68:.0f}" font-size="11.5" '
                f'font-family="{MONO}" fill="{scheme.text}">{_seconds(total)}</text>'
            )
            y += row_h + row_gap

    lx = pad_l
    for pi, phase in enumerate(phases):
        out.append(f'<rect x="{lx:.0f}" y="{height - 30}" width="11" height="11" rx="2" fill="{shades[pi]}"/>')
        out.append(
            f'<text x="{lx + 16:.0f}" y="{height - 20}" font-size="11.5" fill="{scheme.muted}">{_esc(phase)}</text>'
        )
        lx += 20 + 8 * len(phase)
    if caption:
        out.append(f'<text x="12" y="{height - 6}" font-size="11.5" fill="{scheme.muted}">{_esc(caption)}</text>')
    out.append('</svg>')
    return '\n'.join(out)


def write(figures: Iterable[tuple[str, object]], directory: object) -> list[str]:
    """Each figure to ``<name>-light.svg`` and ``<name>-dark.svg``."""
    from pathlib import Path

    out = []
    root = Path(str(directory))
    root.mkdir(parents=True, exist_ok=True)
    for name, figure in figures:
        for scheme in SCHEMES:
            path = root / f'{name}-{scheme.name}.svg'
            body = figure(scheme) if callable(figure) else render(figure, scheme)  # pyrefly: ignore[not-callable]
            path.write_text(body)
            out.append(str(path))
    return out
