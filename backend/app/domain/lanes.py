"""Lane keys: the statistical buckets a load belongs to.

PRD section 7 defines four rungs, narrow to wide — ``ZIP3`` → ``METRO`` →
``REGION`` → ``REGION_ANY``. This module answers one question, purely: **given a
load, which buckets does it belong to?** Phase 4 needs exactly that, twice —
once to know what a change dirties, and once to know what to rebuild. Phase 5's
tier walk asks the mirror-image question — given a *query* load, which buckets do
I read, in what order — and :func:`query_keys_for_load` answers it from the same
constants, so the buckets ingestion writes and the buckets pricing reads cannot
drift apart. The walk itself lives in :mod:`app.domain.pricing`.

Two things worth stating out loud, because both are easy to get subtly wrong:

* **Every tier carries an equipment dimension, and every tier also carries an
  ``ANY`` pool.** Equipment is a hard filter at all three real tiers
  (DECISIONS.md D6) — but a load whose *own* equipment is ``UNKNOWN`` skips the
  filter and is answered from the mixed pool, at whichever tier it lands on. So
  the mixed pool has to exist at ``ZIP3`` and ``METRO`` too, not only on the
  fourth rung. A load contributes to both its own equipment's bucket and the
  ``ANY`` bucket at each tier; a load whose equipment is literally ``UNKNOWN``
  contributes to the ``UNKNOWN`` bucket, which is a real pool of
  equipment-unknown loads and is *not* the same thing as ``ANY``.

* **Geo-null loads contribute no keys at all.** ``keys_for_load`` returns an
  empty tuple for a load that cannot resolve both lane ends, which is the
  normalization table's "excluded from lane stats, still displayed" — the load
  row is written and rendered as usual, it simply backs no statistic. That
  includes the ``REGION`` rungs: a load we cannot place is not evidence about
  the region either.

``RATED_STATUSES`` lives here for the same reason: "which loads back a rate
statistic" is a domain rule, and the repository imports it rather than spelling
a status list into SQL where it could drift.
"""

from __future__ import annotations

from dataclasses import dataclass

from .geo import REGION
from .model import ANY_EQUIPMENT, Equipment, Load, LoadStatus

__all__ = [
    "TIER_ZIP3",
    "TIER_METRO",
    "TIER_REGION",
    "TIER_REGION_ANY",
    "TIERS",
    "RATED_STATUSES",
    "LaneKey",
    "keys_for_load",
    "query_equipment",
    "query_keys_for_load",
]

TIER_ZIP3 = "ZIP3"
TIER_METRO = "METRO"
TIER_REGION = "REGION"
TIER_REGION_ANY = "REGION_ANY"

#: Narrowest to widest — the order the tier walk tries them in (PRD section 7).
TIERS: tuple[str, ...] = (TIER_ZIP3, TIER_METRO, TIER_REGION, TIER_REGION_ANY)

#: The statuses whose carrier rate is a *booked* rate and therefore evidence of
#: what this lane pays. ``PLANNED`` and ``ACTIVE`` are deliberately absent: a
#: day-11 load looking for a truck has no carrier rate, and a load quoted but
#: never covered is not a price anyone was paid. Loads outside this set are
#: still stored, still displayed, and still answered *for* — they just do not
#: vote on the median.
RATED_STATUSES: tuple[LoadStatus, ...] = (
    LoadStatus.COVERED,
    LoadStatus.IN_TRANSIT,
    LoadStatus.DELIVERED,
    LoadStatus.COMPLETED,
)


@dataclass(frozen=True, slots=True)
class LaneKey:
    """One bucket: a tier, an origin/destination key pair, and an equipment pool.

    Hashable, so a file's dirty keys are a ``set``. ``equipment`` is one of the
    four canonical types or :data:`~app.domain.model.ANY_EQUIPMENT`, matching the
    ``CHECK`` on ``lane_stats.equipment``.
    """

    tier: str
    origin_key: str
    dest_key: str
    equipment: str

    @property
    def lane_key(self) -> str:
        """The single-column form ``carrier_stats.lane_key`` stores.

        ``lane_stats`` keeps the pair in two columns because it is queried by
        pair; ``carrier_stats`` is only ever looked up whole, so one column is
        enough. Both are built here so the two tables cannot disagree about what
        a lane is called.
        """
        return f"{self.origin_key}->{self.dest_key}"


def keys_for_load(load: Load) -> tuple[LaneKey, ...]:
    """Every bucket this load is evidence for. Empty when the load is geo-null.

    The result is deliberately *not* filtered by status or by whether the load
    has a rate: these are the keys a change to this load dirties, and a load
    that just lost its carrier rate has to dirty the same keys it used to
    contribute to. Whether it counts toward the numbers is the rebuild's
    question, not the key's.
    """
    if not load.is_lane_resolvable:
        return ()

    origin = load.origin.location
    dest = load.destination.location
    equipment = str(load.equipment)

    # A resolved location always carries both a zip3 and a metro — Place derives
    # zip3 from a validated 5-digit zip and takes its metro from the table group
    # — so is_lane_resolvable is enough to guarantee every key below is non-null.
    pairs = (
        (TIER_ZIP3, origin.zip3, dest.zip3),
        (TIER_METRO, origin.metro, dest.metro),
        (TIER_REGION, REGION, REGION),
    )

    keys: list[LaneKey] = []
    for tier, origin_key, dest_key in pairs:
        keys.append(LaneKey(tier, origin_key, dest_key, equipment))
        keys.append(LaneKey(tier, origin_key, dest_key, ANY_EQUIPMENT))
    keys.append(LaneKey(TIER_REGION_ANY, REGION, REGION, ANY_EQUIPMENT))
    return tuple(keys)


def query_equipment(load: Load) -> str:
    """The equipment pool a tier walk for this load reads at rungs 1-3.

    The load's own type, except that ``UNKNOWN`` skips the filter entirely and
    reads the mixed pool (DECISIONS.md D6). That is invariant 5 expressed as a
    query: a load whose equipment nobody recorded must not quietly become a
    dry-van question, and it must not be answered from the pool of *other*
    equipment-unknown loads either — those share a gap, not a trailer.
    """
    return ANY_EQUIPMENT if load.equipment is Equipment.UNKNOWN else str(load.equipment)


def query_keys_for_load(load: Load) -> tuple[LaneKey | None, ...]:
    """The bucket to read at each rung of :data:`TIERS`, narrow to wide.

    The mirror image of :func:`keys_for_load`: that one answers "which buckets is
    this load *evidence for*", this one answers "which buckets do I *read* to
    answer for it". The result is positional — one entry per element of
    :data:`TIERS`, in the same order — so the walk can report every rung it tried
    even when a rung was unreachable.

    ``None`` at a rung means the load's own geography cannot form that key, which
    happens for exactly one reason: a **geo-null** lane end. Such a load backs no
    statistic (``keys_for_load`` returns nothing for it) but it is still a real
    load that still needs an answer, so its walk starts at ``REGION`` instead of
    at ``ZIP3``. The two region rungs are always formable, because they are keyed
    on the region itself rather than on where this load happens to be — what they
    require placed is the *history*, and that is the repository's population
    filter, not this key.
    """
    equipment = query_equipment(load)
    zip3_key: LaneKey | None = None
    metro_key: LaneKey | None = None
    if load.is_lane_resolvable:
        origin = load.origin.location
        dest = load.destination.location
        zip3_key = LaneKey(TIER_ZIP3, origin.zip3, dest.zip3, equipment)
        metro_key = LaneKey(TIER_METRO, origin.metro, dest.metro, equipment)
    keys: tuple[LaneKey | None, ...] = (
        zip3_key,
        metro_key,
        LaneKey(TIER_REGION, REGION, REGION, equipment),
        # Rung 4 drops the equipment filter by definition, whatever the load is.
        LaneKey(TIER_REGION_ANY, REGION, REGION, ANY_EQUIPMENT),
    )
    assert len(keys) == len(TIERS), "query keys must align with TIERS"
    return keys
