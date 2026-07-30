"""Ingestion: sync files on disk into the canonical model, in order.

Orchestration only. Every byte parsed goes through an adapter, every statement
goes through the repository, and the lane keys come from the domain — this layer
decides *what happens in which order*, which is where invariants 3 and 4 live.

``discovery`` is TASKS.md I1 (find the files, put them in order); ``pipeline`` is
I2-I8 (one file at a time, append-only events, upsert, re-sum, rebuild).
"""

from .discovery import DiscoveredSync, discover_sync_files
from .pipeline import (
    DEFAULT_DATA_ROOT,
    FileOutcome,
    IngestionReport,
    ingest_all,
    ingest_sync_file,
)

__all__ = [
    "DEFAULT_DATA_ROOT",
    "DiscoveredSync",
    "FileOutcome",
    "IngestionReport",
    "discover_sync_files",
    "ingest_all",
    "ingest_sync_file",
]
