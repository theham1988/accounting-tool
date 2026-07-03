"""Server-rendered trend charts: inline SVG sparklines + clickable CSS bars.

ADR-0004 decision 5: trends render as server-side geometry — Python computes
the polyline coordinates and bar heights, the templates emit the result as
inline ``<svg>`` and styled ``<a>`` elements. No JavaScript chart library, no
vendored JS, no build step; the page draws with JavaScript disabled.

Interactivity is navigational, not hover-based: each bar is a link that
drills into its bucket's Period/Month view, so a trend is a navigation
surface, not just a picture.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from html import escape

#: Sparkline viewbox. The SVG scales to its container via CSS; these only fix
#: the coordinate system the polyline is computed in.
SVG_WIDTH = 560
SVG_HEIGHT = 120
#: Vertical padding inside the viewbox so the polyline's extremes are not
#: clipped by the viewbox edge.
_PAD = 6


@dataclass(frozen=True)
class ChartPoint:
    """One bucket's plotted value.

    ``href`` is the drill-in target (the bucket's Period/Month URL); empty
    means the bar renders as a plain, non-clickable bar (e.g. the day-of-week
    breakdown, which compares rather than navigates).
    """

    label: str
    value: Decimal
    href: str = ""
    #: The partner-visible number for the bar (defaults to the raw value).
    display: str = ""


def _scale(values: list[Decimal]) -> tuple[Decimal, Decimal]:
    """The chart's value range: always includes zero so bars have a baseline.

    A flat all-equal series still gets a non-zero range (rendering mid-chart)
    rather than dividing by zero.
    """
    lo = min(values + [Decimal("0")])
    hi = max(values + [Decimal("0")])
    if lo == hi:
        hi = lo + Decimal("1")
    return lo, hi


def sparkline_svg(points: list[ChartPoint], *, css_class: str = "trend-sparkline") -> str:
    """An inline ``<svg>`` polyline through the points, evenly spaced."""
    if not points:
        return ""
    lo, hi = _scale([p.value for p in points])
    span = hi - lo
    inner_h = Decimal(SVG_HEIGHT - 2 * _PAD)
    coords: list[str] = []
    for i, p in enumerate(points):
        x = _PAD if len(points) == 1 else Decimal(_PAD) + (
            Decimal(SVG_WIDTH - 2 * _PAD) * i / (len(points) - 1)
        )
        y = Decimal(_PAD) + inner_h * (hi - p.value) / span
        coords.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg class="{escape(css_class)}" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}"'
        ' role="img" preserveAspectRatio="none">'
        f'<polyline fill="none" points="{" ".join(coords)}" />'
        "</svg>"
    )


def bar_row(points: list[ChartPoint], *, css_class: str = "trend-bars") -> str:
    """A row of CSS bars, one per point, each a drill-in link.

    Bar height is a percentage of the chart's value range (zero-baselined),
    carried as a CSS custom property so the stylesheet owns the visuals. The
    bar's label and value are ordinary text — readable without any styling
    at all.
    """
    if not points:
        return ""
    lo, hi = _scale([p.value for p in points])
    span = hi - lo
    items: list[str] = []
    for p in points:
        pct = (p.value - lo) / span * 100
        display = p.display or str(p.value)
        body = (
            f'<span class="trend-bar__fill" style="--bar-height: {pct:.1f}%"></span>'
            f'<span class="trend-bar__value">{escape(display)}</span>'
            f'<span class="trend-bar__label">{escape(p.label)}</span>'
        )
        if p.href:
            items.append(
                f'<a class="trend-bar" href="{escape(p.href)}">{body}</a>'
            )
        else:
            items.append(f'<span class="trend-bar">{body}</span>')
    return f'<div class="{escape(css_class)}">{"".join(items)}</div>'


__all__ = ["ChartPoint", "SVG_HEIGHT", "SVG_WIDTH", "bar_row", "sparkline_svg"]
