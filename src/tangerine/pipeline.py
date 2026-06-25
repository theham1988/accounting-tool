"""Pipeline entrypoint.

Wires ingestion -> margin. Real sources replace `SeededSource` in later slices;
the call shape stays identical.
"""

from __future__ import annotations

from datetime import date

from .ingestion import Source
from .margin import compute_daily_margin
from .types import DailyMargin


def run(source: Source, day: date) -> DailyMargin:
    """Run the pipeline for a single day and return the daily margin."""
    return compute_daily_margin(source, day)
