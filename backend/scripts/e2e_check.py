"""H1 -- the end-to-end check (TASKS.md H1), and the demo script for the review call.

**One command, from nothing.** This script creates a brand-new Postgres database on
the running server -- never ``carrier_pool`` (the compose default) or any other
database another suite might be using -- bootstraps the schema on it, ingests all 132
sync files under ``data/`` chronologically, and asks the same domain code the API routes
call (``walk_tiers`` / ``rank_carriers`` / ``estimate_price``) for a handful of named
day-11 loads. It is the one place in the repo that proves the whole path a reviewer's
``docker compose up`` exercises: schema -> ingest -> tier walk -> score -> price.

**Every expected number below is copied from ``data/TRACEABILITY.md``**, which was
computed by ``backend/scripts/generate_data.py``'s independent reference model before
any of `app.domain` existed (see that file's header for the provenance argument). The
one exception is the confidence label on the UNKNOWN-equipment load, which
`TRACEABILITY.md` explicitly leaves as an open question ("this may be too confident")
that `DECISIONS.md` D15 later resolved to a medium cap -- so that one expectation is
sourced from D15, not from the traceability table, and is called out as such below.

**Why this bypasses the FastAPI/HTTP layer.** `app.main`'s lifespan and
`app.repository.db`'s ``DEFAULT_DATABASE_URL`` / ``DEFAULT_ADMIN_DATABASE_URL`` are
resolved once, at import time, from the process environment. Booting the ASGI app
here would bootstrap and ingest *the default database* as a side effect of importing
it -- exactly the kind of process-wide surprise CLAUDE.md's carried-defect note warns
about (an earlier conftest mutated module globals and silently redirected every suite).
Calling the domain functions directly, against connections this script opens itself
with an explicit URL, touches nothing another test file has already imported.

Run as a script::

    cd backend && ./.venv/bin/python -m scripts.e2e_check

Run as a test (thin wrapper, same function, see ``tests/integration/test_end_to_end.py``)::

    cd backend && ./.venv/bin/python -m pytest tests/integration/test_end_to_end.py -q -s
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.domain.model import ANY_EQUIPMENT  # noqa: E402
from app.domain.pricing import estimate_price, walk_tiers  # noqa: E402
from app.domain.scoring import rank_carriers  # noqa: E402
from app.ingestion import ingest_all  # noqa: E402
from app.repository import (  # noqa: E402
    BrokerRepository,
    DEFAULT_ADMIN_DATABASE_URL,
    DEFAULT_DATABASE_URL,
    bootstrap,
    connect,
    connect_admin,
    list_brokers,
)

__all__ = ["run", "main"]

REPO_ROOT = _BACKEND_ROOT.parent
DATA_ROOT = REPO_ROOT / "data"

#: Never an existing database. A fixed, greppable name so a stray run is easy to find
#: and drop by hand; recreated (drop-if-exists, then create) at the start of every run
#: so "fresh" does not depend on nobody having touched it before.
E2E_DB_NAME = "carrier_pool_e2e_check"

# Corpus-wide numbers, independent of any single load (TASKS.md I5 -- the same
# idempotency assertion already proven in tests/integration/test_ingestion.py).
EXPECTED_FILES = 132
EXPECTED_LOAD_COUNT = 295
EXPECTED_CARRIER_RATE_SUM = 150716.43
EXPECTED_CUSTOMER_RATE_SUM = 186840.92
_MONEY_TOLERANCE = 0.01  # a cent -- these are sums of NUMERIC dollar amounts


class CheckFailed(AssertionError):
    """Raised with the specific number that disagreed, never swallowed."""


# ---------------------------------------------------------------------------
# Fresh-database lifecycle
# ---------------------------------------------------------------------------


def _with_dbname(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def _maintenance_url() -> str:
    """The admin credential, pointed at ``postgres`` instead of a real database --
    the one database guaranteed to exist, and the only place ``CREATE``/``DROP
    DATABASE`` can be issued from."""
    return _with_dbname(DEFAULT_ADMIN_DATABASE_URL, "postgres")


def _drop_e2e_database() -> None:
    with psycopg.connect(_maintenance_url(), autocommit=True) as conn:
        # WITH (FORCE) (PG 13+) disconnects anyone still attached instead of
        # failing -- a crashed previous run must not block a fresh one.
        conn.execute(f"DROP DATABASE IF EXISTS {E2E_DB_NAME} WITH (FORCE)")


def _create_e2e_database() -> None:
    with psycopg.connect(_maintenance_url(), autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {E2E_DB_NAME}")


def _bootstrap_fresh() -> str:
    """Create the database, apply schema.sql, and return the app-role URL."""
    _drop_e2e_database()
    _create_e2e_database()
    admin_url = _with_dbname(DEFAULT_ADMIN_DATABASE_URL, E2E_DB_NAME)
    app_url = _with_dbname(DEFAULT_DATABASE_URL, E2E_DB_NAME)
    with connect_admin(admin_url) as conn:
        bootstrap(conn)
    return app_url


# ---------------------------------------------------------------------------
# Expectations, sourced from data/TRACEABILITY.md (noted inline where a
# different document -- DECISIONS.md -- is what actually settles the number)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedLoad:
    label: str
    broker_id: str
    source_load_id: str
    #: Every rung the walk must try, in order: (tier, load_count, accepted).
    expected_rungs: tuple[tuple[str, int, bool], ...]
    expected_tier: str
    expected_load_count: int
    expected_confidence: str
    expected_top_carrier: str
    expected_top_score: float
    expected_p25: float
    expected_p50: float
    expected_p75: float
    expected_point_usd: float
    expected_low_usd: float
    expected_high_usd: float
    #: Substrings that must appear, verbatim, in the provenance sentence.
    provenance_contains: tuple[str, ...]
    note: str = ""


EXPECTED_LOADS: tuple[ExpectedLoad, ...] = (
    # --- Rung 1: ZIP3 clears the minimum on the first try (TRACEABILITY.md
    # broker_a DAY11-RICH, "the narrowest tier clears the 5-load minimum").
    ExpectedLoad(
        label="DAY11-RICH",
        broker_id="broker_a",
        source_load_id="127412794",
        expected_rungs=(("ZIP3", 12, True),),
        expected_tier="ZIP3",
        expected_load_count=12,
        expected_confidence="medium",
        expected_top_carrier="IBRAHIM TRANSPORT INC",
        expected_top_score=84.3,
        expected_p25=1.7500,
        expected_p50=1.7800,
        expected_p75=1.8050,
        expected_point_usd=526.88,
        expected_low_usd=518.00,
        expected_high_usd=534.28,
        provenance_contains=("12 loads", "750->774", "medium confidence"),
    ),
    # --- Rung 2: ZIP3 too thin (2), METRO clears it (TRACEABILITY.md broker_c
    # DAY11-UNKNOWN-EQUIP). Also the D15 heterogeneous-pool case: the load's
    # equipment is null so no rung filters by equipment, the accepted pool
    # mixes dry van and reefer, and confidence is capped. TRACEABILITY.md
    # itself computed **high** here and flagged the cap as an open question
    # for Phase 5 ("this may be too confident... whichever is chosen, it
    # should be a recorded decision"); DECISIONS.md D15 is the record that
    # settled it at **medium**, so that one field is sourced from D15, not
    # from the traceability table.
    ExpectedLoad(
        label="DAY11-UNKNOWN-EQUIP",
        broker_id="broker_c",
        source_load_id="a0jO900000RE5kFMEU",
        expected_rungs=(("ZIP3", 2, False), ("METRO", 31, True)),
        expected_tier="METRO",
        expected_load_count=31,
        expected_confidence="medium",  # DECISIONS.md D15, not TRACEABILITY.md's "high"
        expected_top_carrier="Metroplex Ridge Logistics, Inc.",
        expected_top_score=80.6,
        expected_p25=2.4800,
        expected_p50=2.5300,
        expected_p75=2.6700,
        expected_point_usd=742.30,
        expected_low_usd=727.63,
        expected_high_usd=783.38,
        provenance_contains=(
            "31 loads",
            "DFW->HOU",
            "23 dry van",
            "8 reefer",
            "capped at medium",
            "medium confidence",
        ),
        note="confidence expectation is DECISIONS.md D15, which supersedes TRACEABILITY.md's uncapped 'high'",
    ),
    # --- Rung 3: ZIP3 and METRO both too thin, REGION clears it on 70 loads,
    # labelled low confidence rather than hidden (TRACEABILITY.md broker_a
    # DAY11-THIN).
    ExpectedLoad(
        label="DAY11-THIN",
        broker_id="broker_a",
        source_load_id="127413097",
        expected_rungs=(("ZIP3", 2, False), ("METRO", 2, False), ("REGION", 70, True)),
        expected_tier="REGION",
        expected_load_count=70,
        expected_confidence="low",
        expected_top_carrier="RIO GRANDE HAULING CO",
        expected_top_score=85.9,
        expected_p25=1.7850,
        expected_p50=1.8800,
        expected_p75=2.0200,
        expected_point_usd=351.94,
        expected_low_usd=334.15,
        expected_high_usd=378.14,
        provenance_contains=("70 loads", "TX_TRIANGLE", "low confidence"),
    ),
    # --- Rung 4: only 4 flatbed loads region-wide, so even REGION falls
    # short and the walk drops the equipment filter entirely (TRACEABILITY.md
    # broker_b DAY11-COLDSTART). Low confidence by rule, not by count.
    ExpectedLoad(
        label="DAY11-COLDSTART",
        broker_id="broker_b",
        source_load_id="HD-2026-005077",
        expected_rungs=(
            ("ZIP3", 1, False),
            ("METRO", 2, False),
            ("REGION", 4, False),
            ("REGION_ANY", 93, True),
        ),
        expected_tier="REGION_ANY",
        expected_load_count=93,
        expected_confidence="low",
        expected_top_carrier="NORTH TEXAS LINE HAUL INC",
        expected_top_score=77.7,
        expected_p25=2.1500,
        expected_p50=2.3200,
        expected_p75=2.5100,
        expected_point_usd=502.05,
        expected_low_usd=465.26,
        expected_high_usd=543.16,
        provenance_contains=("93 loads", "TX_TRIANGLE (any equipment)", "for a flatbed load", "low confidence"),
    ),
)


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def _close(actual: float | None, expected: float, *, places: int = 4, what: str = "") -> None:
    if actual is None or round(actual, places) != round(expected, places):
        raise CheckFailed(f"{what}: expected {expected}, got {actual}")


@dataclass(frozen=True)
class LoadCheckResult:
    label: str
    lines: tuple[str, ...]


def check_load(conn: psycopg.Connection, exp: ExpectedLoad) -> LoadCheckResult:
    repo = BrokerRepository(conn, exp.broker_id)
    load = repo.get_load(exp.source_load_id)
    if load is None:
        raise CheckFailed(f"{exp.label}: no load {exp.source_load_id} for {exp.broker_id} after ingest")

    walk = walk_tiers(load, repo.lane_stats_for)
    actual_rungs = tuple((r.tier, r.load_count, r.accepted) for r in walk.rungs)
    if actual_rungs != exp.expected_rungs:
        raise CheckFailed(
            f"{exp.label}: tier walk mismatch -- expected {exp.expected_rungs}, got {actual_rungs}"
        )
    if walk.tier != exp.expected_tier:
        raise CheckFailed(f"{exp.label}: accepted tier {walk.tier!r} != expected {exp.expected_tier!r}")
    if walk.load_count != exp.expected_load_count:
        raise CheckFailed(
            f"{exp.label}: accepted load count {walk.load_count} != expected {exp.expected_load_count}"
        )

    key = None if walk.accepted is None else walk.accepted.key
    ranking = rank_carriers(load, walk, repo.ranking_inputs(key))
    if not ranking.carriers:
        raise CheckFailed(f"{exp.label}: no carriers returned at all")
    top = ranking.carriers[0]
    if top.carrier.name != exp.expected_top_carrier:
        raise CheckFailed(
            f"{exp.label}: top carrier {top.carrier.name!r} != expected {exp.expected_top_carrier!r}"
        )
    if abs(top.score - exp.expected_top_score) > 0.05:
        raise CheckFailed(f"{exp.label}: top score {top.score} != expected {exp.expected_top_score}")

    estimate = estimate_price(load, lane_stats=repo.lane_stats_for, equipment_mix=repo.equipment_mix_for)
    if estimate.confidence.value != exp.expected_confidence:
        raise CheckFailed(
            f"{exp.label}: confidence {estimate.confidence.value!r} != expected {exp.expected_confidence!r}"
        )
    _close(estimate.rate_per_mile_p25, exp.expected_p25, what=f"{exp.label} p25")
    _close(estimate.rate_per_mile_p50, exp.expected_p50, what=f"{exp.label} median")
    _close(estimate.rate_per_mile_p75, exp.expected_p75, what=f"{exp.label} p75")
    _close(estimate.point_usd, exp.expected_point_usd, places=2, what=f"{exp.label} point $")
    _close(estimate.low_usd, exp.expected_low_usd, places=2, what=f"{exp.label} low $")
    _close(estimate.high_usd, exp.expected_high_usd, places=2, what=f"{exp.label} high $")
    for phrase in exp.provenance_contains:
        if phrase not in estimate.provenance:
            raise CheckFailed(f"{exp.label}: provenance missing {phrase!r} -- got: {estimate.provenance!r}")

    lines = [
        f"== {exp.label} -- {exp.broker_id} / {exp.source_load_id} ==",
        "  tier walk: "
        + " -> ".join(
            f"{tier} {count} ({'ACCEPTED' if accepted else f'rejected, {count} < 5'})"
            for tier, count, accepted in actual_rungs
        ),
        f"  accepted tier: {walk.tier}, {walk.load_count} loads back the estimate",
        f"  top carrier: {top.carrier.name} (score {top.score})",
    ]
    for reason in top.reasons:
        lines.append(f"    - {reason}")
    lines.append(
        f"  price estimate: ${estimate.point_usd:.2f} "
        f"(range ${estimate.low_usd:.2f} - ${estimate.high_usd:.2f}), "
        f"confidence {estimate.confidence.value}"
    )
    lines.append(f"  provenance: {estimate.provenance!r}")
    if exp.note:
        lines.append(f"  note: {exp.note}")
    lines.append("  PASS")
    return LoadCheckResult(label=exp.label, lines=tuple(lines))


def check_corpus(conn: psycopg.Connection) -> tuple[str, ...]:
    brokers = list_brokers(conn)
    total_loads = 0
    carrier_sum = 0.0
    customer_sum = 0.0
    per_broker: dict[str, int] = {}
    for broker in brokers:
        repo = BrokerRepository(conn, broker.id)
        loads = repo.list_loads()
        per_broker[broker.id] = len(loads)
        total_loads += len(loads)
        carrier_sum += sum(l.carrier_rate or 0.0 for l in loads)
        customer_sum += sum(l.customer_rate or 0.0 for l in loads)

    if total_loads != EXPECTED_LOAD_COUNT:
        raise CheckFailed(f"corpus: {total_loads} loads != expected {EXPECTED_LOAD_COUNT}")
    if abs(carrier_sum - EXPECTED_CARRIER_RATE_SUM) > _MONEY_TOLERANCE:
        raise CheckFailed(
            f"corpus: sum(carrier_rate) {carrier_sum:.2f} != expected {EXPECTED_CARRIER_RATE_SUM}"
        )
    if abs(customer_sum - EXPECTED_CUSTOMER_RATE_SUM) > _MONEY_TOLERANCE:
        raise CheckFailed(
            f"corpus: sum(customer_rate) {customer_sum:.2f} != expected {EXPECTED_CUSTOMER_RATE_SUM}"
        )
    return (
        f"  {total_loads} loads across {len(brokers)} brokers "
        f"({', '.join(f'{bid} {n}' for bid, n in per_broker.items())})",
        f"  sum(carrier_rate)  = ${carrier_sum:,.2f} (expected ${EXPECTED_CARRIER_RATE_SUM:,.2f})",
        f"  sum(customer_rate) = ${customer_sum:,.2f} (expected ${EXPECTED_CUSTOMER_RATE_SUM:,.2f})",
    )


@dataclass(frozen=True)
class Report:
    """The whole run, printable and assertable."""

    files_ingested: int
    files_skipped: int
    replay_files_ingested: int
    replay_files_skipped: int
    corpus_lines: tuple[str, ...]
    load_results: tuple[LoadCheckResult, ...]
    elapsed_seconds: float

    def render(self) -> str:
        lines = [
            "H1 end-to-end check -- fresh database, 132 files, 4 named day-11 loads (one per tier rung)",
            "",
            f"== Ingestion ({self.files_ingested} ingested, {self.files_skipped} skipped) ==",
            *self.corpus_lines,
            f"  idempotency re-run: {self.replay_files_ingested} ingested, "
            f"{self.replay_files_skipped} skipped (sums unchanged, re-verified above)",
            "",
        ]
        for result in self.load_results:
            lines.extend(result.lines)
            lines.append("")
        lines.append(f"ALL CHECKS PASSED in {self.elapsed_seconds:.1f}s")
        return "\n".join(lines)


def run(*, keep_db: bool = False) -> Report:
    """The whole check. Raises :class:`CheckFailed` (or a bare ``AssertionError``)
    on the first disagreement -- nothing here weakens an assertion to get green."""
    started = time.monotonic()
    app_url = _bootstrap_fresh()
    try:
        conn = connect(app_url)
        try:
            report = ingest_all(conn, DATA_ROOT)
            if report.files_ingested != EXPECTED_FILES or report.files_skipped != 0:
                raise CheckFailed(
                    f"ingest: {report.files_ingested} ingested / {report.files_skipped} "
                    f"skipped != expected {EXPECTED_FILES} ingested / 0 skipped"
                )
            corpus_lines = check_corpus(conn)

            # Idempotency (TASKS.md I5): re-ingesting the same 132 files must be a
            # no-op on both counts and money -- a double-counted TMS B line item
            # would only show up in the sums, never in the row counts.
            replay = ingest_all(conn, DATA_ROOT)
            if replay.files_ingested != 0 or replay.files_skipped != EXPECTED_FILES:
                raise CheckFailed(
                    f"idempotency: re-ingest gave {replay.files_ingested} ingested / "
                    f"{replay.files_skipped} skipped != expected 0 / {EXPECTED_FILES}"
                )
            check_corpus(conn)  # same sums, asserted again after the replay

            load_results = tuple(check_load(conn, exp) for exp in EXPECTED_LOADS)
        finally:
            conn.close()
    finally:
        if not keep_db:
            _drop_e2e_database()

    elapsed = time.monotonic() - started
    return Report(
        files_ingested=report.files_ingested,
        files_skipped=report.files_skipped,
        replay_files_ingested=replay.files_ingested,
        replay_files_skipped=replay.files_skipped,
        corpus_lines=corpus_lines,
        load_results=load_results,
        elapsed_seconds=elapsed,
    )


def main(argv: list[str] | None = None) -> int:
    keep_db = "--keep-db" in (argv if argv is not None else sys.argv[1:])
    try:
        report = run(keep_db=keep_db)
    except CheckFailed as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(report.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
