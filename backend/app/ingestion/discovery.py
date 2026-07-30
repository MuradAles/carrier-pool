"""Finding the sync files and putting them in order (TASKS.md I1).

One rule, and it is CLAUDE.md invariant 4: **sort by the timestamp in the
filename**, not by the timestamp inside the file, not by directory listing
order, not by modification time.

**Why sorting one global list across three directories is legitimate.** The
three TMSs write their payload timestamps in three different zones — TMS A
carries an offset, TMS B is naive US Central, TMS C is UTC — so the envelope
timestamps are not directly comparable without conversion. The *filenames* are.
All three name their files on the same local Central clock
(``2026-07-06T18-00_sync.json`` means 18:00 Central for every TMS), which is the
only reason a single chronological order across the three streams means
anything. That assumption is stated here because it is invisible in the code:
the sort looks like it would work for any three directories, and it would not.

Cross-broker order is in any case a determinism property rather than a
correctness one — brokers are isolated tenants and one's files cannot change
another's numbers — so files sharing a timestamp are broken by broker id to make
a run reproducible.

A file whose name does not match the pattern raises rather than being skipped: a
stray file in a TMS directory is either data we would be silently dropping or a
mistake, and both deserve a stack trace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..adapters import adapter_for
from ..domain.model import Broker

__all__ = ["SYNC_FILE_PATTERN", "DiscoveredSync", "discover_sync_files"]

#: ``{YYYY-MM-DD}T{HH-MM}_sync.json`` (PRD section 4).
SYNC_FILE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})_sync\.json$")

_FILENAME_FORMAT = "%Y-%m-%dT%H-%M"


@dataclass(frozen=True, slots=True)
class DiscoveredSync:
    """One sync file, with the broker it belongs to and its place in the order."""

    broker: Broker
    sync_file: str
    path: Path
    #: The filename's timestamp, read as the local Central wall clock all three
    #: TMSs name their files on. Kept naive on purpose: it is a sort key and a
    #: statement about a shared clock, not an instant. The instant a file states
    #: about itself is ``AdaptedSync.synced_at``, which the adapter produces in
    #: UTC from the payload.
    filename_at: datetime


def _filename_at(name: str, path: Path) -> datetime:
    match = SYNC_FILE_PATTERN.match(name)
    if match is None:
        raise ValueError(
            f"{path}: filename does not match {{YYYY-MM-DD}}T{{HH-MM}}_sync.json, so "
            "it has no place in the chronological order (CLAUDE.md invariant 4)"
        )
    day, hour, minute = match.groups()
    return datetime.strptime(f"{day}T{hour}-{minute}", _FILENAME_FORMAT)


def discover_sync_files(
    data_root: Path, brokers: list[Broker]
) -> list[DiscoveredSync]:
    """Every broker's sync files, as one chronologically ordered list.

    Each broker's directory comes from its TMS adapter (``adapter.data_dir``), so
    the mapping from tenant to bytes on disk has one definition. A broker whose
    directory is missing contributes nothing and does not fail the run — two
    brokers' data being present is not a reason to refuse to ingest it.
    """
    found: list[DiscoveredSync] = []
    for broker in brokers:
        directory = data_root / adapter_for(broker.tms_type).data_dir
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or not path.name.endswith(".json"):
                continue
            found.append(
                DiscoveredSync(
                    broker=broker,
                    sync_file=path.name,
                    path=path,
                    filename_at=_filename_at(path.name, path),
                )
            )
    # The filename timestamp first — the whole invariant — then broker id purely
    # so that a run is reproducible when three files share a slot.
    found.sort(key=lambda s: (s.filename_at, s.broker.id))
    return found
