"""Receipt ingestion boundary (slice 03).

Mirrors `ingestion.Source` for the receipt side: a protocol that yields
extracted receipts, plus a seeded in-repo implementation used by tests and by
the `python -m tangerine` runner. Real implementations (direct upload +
LLM/OCR provider, or Google Drive import + OCR) plug in here without touching
the check/approval engines.

The OCR/LLM provider is a genuine external boundary (PRD testing rule: only
mock genuine external boundaries). `OcrProvider` is therefore a separate
protocol from `ReceiptSource`: a real `ReceiptSource` composes an upload
source with an `OcrProvider`, while tests and seeded fixtures bypass both by
constructing `ExtractedReceipt` payloads directly.
"""

from __future__ import annotations

from typing import Protocol

from .types import ExtractedReceipt


class OcrProvider(Protocol):
    """External OCR/LLM boundary: raw bytes -> structured receipt.

    Real implementations call a provider (PRD open item: choice of LLM/OCR
    provider is deferred). Slice 03 does not exercise this boundary — tests
    feed `ExtractedReceipt` payloads directly via `SeededReceiptSource` — but
    the seam is here so a later slice can swap in a real provider.
    """

    def extract(self, raw: bytes) -> ExtractedReceipt: ...


class ReceiptSource(Protocol):
    """Read-side boundary for the receipt pipeline.

    A source yields the extracted receipts the checker consumes. Concrete
    sources (seeded fixtures today; Google Drive import + OCR later) satisfy
    this protocol.
    """

    def receipts(self) -> list[ExtractedReceipt]: ...


class SeededReceiptSource:
    """In-memory receipt source built from explicit extracted payloads."""

    def __init__(self, receipts: list[ExtractedReceipt]) -> None:
        self._receipts = list(receipts)

    def receipts(self) -> list[ExtractedReceipt]:
        return list(self._receipts)
