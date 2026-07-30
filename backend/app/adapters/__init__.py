"""Adapter layer: one TMS's native JSON in, canonical objects out.

Thin by construction. An adapter parses and converts; it never scores, never
computes a lane key, never opens a connection. Everything it needs to make a
judgment call is in the file it was handed plus the offline geo table, which is
why the whole layer is unit-testable with no database and no fixtures beyond a
dict.

``adapter_for(tms_type)`` resolves ``brokers.tms_type`` — ``'A'``, ``'B'``,
``'C'`` — to the implementation. Ingestion (Phase 4) is the only intended caller;
it reads the broker row and never names an adapter class directly.
"""

from __future__ import annotations

from .base import AdaptedRecord, AdaptedSync, AdapterError, SyncBuilder, TmsAdapter
from .tms_a import TmsAFreightFlowAdapter
from .tms_b import TmsBHaulDeskAdapter
from .tms_c import TmsCBrokerOsAdapter

__all__ = [
    "AdapterError",
    "AdaptedRecord",
    "AdaptedSync",
    "SyncBuilder",
    "TmsAdapter",
    "TmsAFreightFlowAdapter",
    "TmsBHaulDeskAdapter",
    "TmsCBrokerOsAdapter",
    "ADAPTERS",
    "adapter_for",
]

#: One instance per TMS. Adapters are stateless, so sharing them is safe and
#: keeps ``adapter_for`` a lookup rather than a constructor call per file.
ADAPTERS: dict[str, TmsAdapter] = {
    adapter.tms_type: adapter
    for adapter in (
        TmsAFreightFlowAdapter(),
        TmsBHaulDeskAdapter(),
        TmsCBrokerOsAdapter(),
    )
}


def adapter_for(tms_type: str) -> TmsAdapter:
    """The adapter for a ``brokers.tms_type``. Raises on an unknown type."""
    try:
        return ADAPTERS[tms_type]
    except KeyError:
        raise AdapterError(
            f"no adapter for tms_type {tms_type!r}; known types are "
            f"{sorted(ADAPTERS)}"
        ) from None
