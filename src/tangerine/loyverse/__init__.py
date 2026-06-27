"""Loyverse API sync subpackage (slice 02).

Public surface:

- ``config``       — credentials + polling cadence
- ``payloads``     — raw Loyverse JSON ``TypedDict`` shapes
- ``http``         — HTTP client (the external boundary)
- ``parser``       — pure raw-payload -> domain-type converters
- ``store``        — storage protocol + in-memory implementation
- ``source``       — adapter to the pipeline's ``Source`` protocol
- ``sync``         — orchestrator tying http + parser + store
"""

from __future__ import annotations
