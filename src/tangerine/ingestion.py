"""Ingestion boundary.

Real integrations (Loyverse API, receipt OCR, keg weighs) plug in here. For
slice 01 the only implementation is a seeded in-repo source, but the boundary
is explicit so later slices swap in real sources without touching the margin
engine or the pipeline.
"""

from __future__ import annotations

from typing import Protocol

from .types import Recipe, Sale


class Source(Protocol):
    """Read-side boundary for the pipeline.

    A source yields the sales and recipes the margin engine consumes. Concrete
    sources (seeded fixtures today; Loyverse + receipt processor later) satisfy
    this protocol.
    """

    def sales(self) -> list[Sale]: ...

    def recipes(self) -> list[Recipe]: ...
