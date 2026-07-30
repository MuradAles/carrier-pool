"""H1 -- the end-to-end check, as a pytest test (TASKS.md H1).

The whole check lives in ``backend/scripts/e2e_check.py`` so it is runnable as a
plain script too (the exact form the README's run section quotes); this file is a
thin wrapper so ``pytest tests/integration -q`` also proves it, and so CI does not
need a second invocation path.

Deliberately **not** parametrized over the fresh-database fixtures the rest of
``tests/integration`` uses (``admin_conn`` / ``clean_db`` / ``app_conn`` in
``conftest.py``): those all point at the one long-lived ``carrier_pool`` database
that every other integration test truncates and shares. H1's whole point is a
database that exists for nobody else -- ``scripts.e2e_check`` creates and drops its
own (``carrier_pool_e2e_check``), so this test does not touch, and is not affected
by, any fixture or state the other 453 tests rely on.
"""

from __future__ import annotations

from scripts.e2e_check import run


def test_end_to_end_fresh_database_ingest_and_day11_answers() -> None:
    """Fresh DB -> ingest all 132 files -> 4 named day-11 loads (one per tier rung)
    match ``data/TRACEABILITY.md`` exactly, plus the corpus-wide load count and
    money sums. See ``scripts/e2e_check.py`` for where every expected number comes
    from and why.
    """
    report = run()
    print("\n" + report.render())  # -s shows the "why it passed" narrative
    assert report.files_ingested == 132
    assert report.files_skipped == 0
    assert report.replay_files_ingested == 0
    assert report.replay_files_skipped == 132
    assert len(report.load_results) == 4
