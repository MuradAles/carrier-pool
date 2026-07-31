-- Carrier Pool schema (PRD section 5).
--
-- Applied at startup by app.repository.bootstrap, inside one transaction, by a
-- connection with owner rights. Every statement is idempotent, so a container
-- restart re-applies it for free (DECISIONS.md D8).
--
-- Two things in here are not just tables:
--
--   1. The UNIQUE on sync_files (broker_id, sync_file) IS the idempotency
--      guarantee for ingestion (D3) — enforced by the database, not by a
--      Python check that a future call site could skip.
--   2. Row-level security. Every tenant table carries a policy keyed on the
--      `app.broker_id` setting, and the application role carrier_pool_app is
--      NOSUPERUSER/NOBYPASSRLS, so a query issued through the repository's
--      broker binding cannot read another broker's rows even if its SQL has no
--      WHERE clause at all (CLAUDE.md invariant 1). With the setting unset the
--      policy's current_setting() call raises, so an unbound query fails closed
--      rather than returning everything.
--
-- Append-only (invariant 3) is enforced the same way: carrier_pool_app is
-- granted SELECT and INSERT on sync_files and sync_events and nothing else, so
-- there is no privilege with which to UPDATE or DELETE an event.

-- ---------------------------------------------------------------------------
-- Tenants
-- ---------------------------------------------------------------------------

-- The only table with no broker_id: it *is* the broker directory, and the UI's
-- broker dropdown has to list all three. It holds no load, carrier, customer or
-- money data, so no policy applies to it.
CREATE TABLE IF NOT EXISTS brokers (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    tms_type TEXT NOT NULL CHECK (tms_type IN ('A', 'B', 'C'))
);

-- PRD section 3 fixes the roster: one broker per TMS.
INSERT INTO brokers (id, name, tms_type) VALUES
    ('broker_a', 'FreightFlow', 'A'),
    ('broker_b', 'HaulDesk',    'B'),
    ('broker_c', 'BrokerOS',    'C')
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Provenance: the append-only log (DECISIONS.md D3)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sync_files (
    id          BIGSERIAL PRIMARY KEY,
    broker_id   TEXT        NOT NULL REFERENCES brokers (id),
    sync_file   TEXT        NOT NULL,
    synced_at   TIMESTAMPTZ NOT NULL,
    raw_json    JSONB       NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Re-ingesting a file is a no-op because this constraint says so.
    UNIQUE (broker_id, sync_file)
);

CREATE TABLE IF NOT EXISTS sync_events (
    id               BIGSERIAL PRIMARY KEY,
    sync_file_id     BIGINT      NOT NULL REFERENCES sync_files (id),
    broker_id        TEXT        NOT NULL REFERENCES brokers (id),
    entity_type      TEXT        NOT NULL
        CHECK (entity_type IN ('LOAD', 'CARRIER', 'CUSTOMER', 'RATE_LINE')),
    source_entity_id TEXT        NOT NULL,
    -- Set on LOAD and RATE_LINE rows, so "every contribution to this load's
    -- money" is one indexed read regardless of which shape it arrived in.
    source_load_id   TEXT,
    raw_json         JSONB       NOT NULL,
    event_seq        INTEGER     NOT NULL,
    -- Copied from the file. Ordering is by the data (synced_at, event_seq),
    -- never by the surrogate id, so an out-of-order ingest cannot reorder a
    -- rebuild.
    synced_at        TIMESTAMPTZ NOT NULL,
    UNIQUE (sync_file_id, event_seq),
    CONSTRAINT sync_events_load_id_present
        CHECK (entity_type NOT IN ('LOAD', 'RATE_LINE') OR source_load_id IS NOT NULL)
);

-- Rebuilding one load's money, and the load-detail sync history panel.
CREATE INDEX IF NOT EXISTS sync_events_load_order_idx
    ON sync_events (broker_id, source_load_id, synced_at, event_seq);

-- DECISIONS.md D3's "second dedupe key beneath the file-level one", made real
-- (D22). A rate line is one immutable money contribution identified by its own
-- rate_id, so the same rate_id arriving in a second file is the *same* line
-- restated, not a second $700. The file-level key cannot see that: an
-- overlapping sync window, or an operator re-pulling a day under a new
-- filename, is a new file carrying an old line. A legitimate TMS B correction
-- is a *new* rate_id with a negative amount, which this index does not touch.
--
-- Only RATE_LINE rows are covered: a LOAD or CARRIER event is a restatement of
-- current truth and is *supposed* to arrive many times.
--
-- The key includes source_load_id (D25) because that is the grain
-- pipeline._rebuild_money dedupes at, and the narrower (broker_id,
-- source_entity_id) form refused a *different* load's rate line whenever a TMS
-- numbers its rate ids per load rather than globally — two loads in one file
-- both carrying rate_id 1, and the second load's money silently never arrives.
-- D22's duplicate is a restatement of one load's own line item, which is
-- same-load by definition, so this key still refuses it.
CREATE UNIQUE INDEX IF NOT EXISTS sync_events_rate_line_load_identity_idx
    ON sync_events (broker_id, source_load_id, source_entity_id)
    WHERE entity_type = 'RATE_LINE';

-- Superseded by the index above. Dropped rather than left in place: it is
-- strictly narrower, so a database created before D25 would keep refusing the
-- rows the new key admits. schema.sql is applied on every start (D8), so this
-- is how an existing database is brought into step; on a fresh one it is a
-- no-op.
DROP INDEX IF EXISTS sync_events_rate_line_identity_idx;

-- ---------------------------------------------------------------------------
-- Current truth, rewritten from the newest event
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS loads (
    id                  BIGSERIAL PRIMARY KEY,
    broker_id           TEXT        NOT NULL REFERENCES brokers (id),
    source_load_id      TEXT        NOT NULL,
    load_number         TEXT,
    status              TEXT        NOT NULL CHECK (status IN (
                            'PLANNED', 'ACTIVE', 'COVERED',
                            'IN_TRANSIT', 'DELIVERED', 'COMPLETED')),
    -- No DEFAULT, by design: invariant 5. An adapter that has nothing to say
    -- must say UNKNOWN out loud.
    equipment           TEXT        NOT NULL
        CHECK (equipment IN ('DRY_VAN', 'REEFER', 'FLATBED', 'UNKNOWN')),
    weight_lbs          NUMERIC(12, 2),
    distance_miles      NUMERIC(10, 2),
    customer_rate       NUMERIC(12, 2),
    carrier_rate        NUMERIC(12, 2),
    -- The one definition of $/mi in the database. Python's
    -- Load.rate_per_mile carries the same expression; a generated column means
    -- every SQL consumer (lane percentiles, carrier averages) reads one column
    -- rather than re-deriving the ratio per query.
    rate_per_mile       NUMERIC GENERATED ALWAYS AS
                            (carrier_rate / NULLIF(distance_miles, 0)) STORED,
    source_carrier_id   TEXT,
    source_customer_id  TEXT,

    -- Lane keys, derived at ingest from the same Stop objects that are written
    -- to `stops` in the same statement. NULL means geo-null: kept out of lane
    -- statistics, still displayed from `stops` (normalization table, Location).
    pickup_zip3         TEXT,
    pickup_metro        TEXT,
    pickup_lat          DOUBLE PRECISION,
    pickup_lon          DOUBLE PRECISION,
    delivery_zip3       TEXT,
    delivery_metro      TEXT,
    delivery_lat        DOUBLE PRECISION,
    delivery_lon        DOUBLE PRECISION,

    -- Local (US Central) calendar dates: the only on-time basis all three
    -- formats support honestly (DECISIONS.md D7/D16).
    pickup_scheduled_date   DATE,
    delivery_scheduled_date DATE,
    pickup_actual_at        TIMESTAMPTZ,
    delivery_actual_at      TIMESTAMPTZ,
    -- Delivered on or before the scheduled delivery date (D7), compared in
    -- Central (D16). NULL is the third outcome, not a miss: no last drop, no
    -- scheduled date, or no arrival yet. Written from
    -- app.domain.localtime.delivered_on_time on every upsert, so the aggregate
    -- SQL below counts a verdict rather than re-deriving one — there is exactly
    -- one implementation of the rule, and it is the Python one.
    delivered_on_time       BOOLEAN,

    -- Full ordered stop list, canonical form. Middle stops are kept here; they
    -- are not lane-forming but they are part of the load.
    stops               JSONB       NOT NULL,
    cargo               JSONB       NOT NULL DEFAULT '[]'::jsonb,

    created_at          TIMESTAMPTZ,
    last_modified_at    TIMESTAMPTZ,
    last_seen_sync_at   TIMESTAMPTZ NOT NULL,

    UNIQUE (broker_id, source_load_id)
);

-- The tier-walk aggregations (PRD section 7), one index per tier that has a key
-- pair. REGION and REGION_ANY scan the broker's loads, which is the same work.
CREATE INDEX IF NOT EXISTS loads_zip3_lane_idx
    ON loads (broker_id, equipment, pickup_zip3, delivery_zip3);
CREATE INDEX IF NOT EXISTS loads_metro_lane_idx
    ON loads (broker_id, equipment, pickup_metro, delivery_metro);
-- Carrier stat rebuilds, and "which loads has this carrier hauled".
CREATE INDEX IF NOT EXISTS loads_carrier_idx
    ON loads (broker_id, source_carrier_id);

CREATE TABLE IF NOT EXISTS carriers (
    id                BIGSERIAL PRIMARY KEY,
    broker_id         TEXT NOT NULL REFERENCES brokers (id),
    source_carrier_id TEXT NOT NULL,
    name              TEXT,
    mc_number         TEXT,
    dot_number        TEXT,
    phone             TEXT,
    home_city         TEXT,
    home_state        TEXT,
    -- Last known delivery position, for the deadhead signal (PRD section 8).
    -- One fact per carrier, so it lives here rather than being copied onto
    -- every carrier_stats lane row where partial rebuilds could disagree.
    last_delivery_lat DOUBLE PRECISION,
    last_delivery_lon DOUBLE PRECISION,
    last_delivery_at  TIMESTAMPTZ,
    UNIQUE (broker_id, source_carrier_id)
);

CREATE TABLE IF NOT EXISTS customers (
    id                 BIGSERIAL PRIMARY KEY,
    broker_id          TEXT NOT NULL REFERENCES brokers (id),
    source_customer_id TEXT NOT NULL,
    name               TEXT,
    UNIQUE (broker_id, source_customer_id)
);

-- ---------------------------------------------------------------------------
-- Derived statistics — rebuilt for dirty keys from the log, never patched
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS lane_stats (
    id                BIGSERIAL PRIMARY KEY,
    broker_id         TEXT NOT NULL REFERENCES brokers (id),
    tier              TEXT NOT NULL
        CHECK (tier IN ('ZIP3', 'METRO', 'REGION', 'REGION_ANY')),
    origin_key        TEXT NOT NULL,
    dest_key          TEXT NOT NULL,
    -- 'ANY' is the fourth rung's pool, which spans every type (D6).
    equipment         TEXT NOT NULL
        CHECK (equipment IN ('DRY_VAN', 'REEFER', 'FLATBED', 'UNKNOWN', 'ANY')),
    load_count        INTEGER NOT NULL,
    rate_per_mile_p25 NUMERIC(10, 4),
    rate_per_mile_p50 NUMERIC(10, 4),
    rate_per_mile_p75 NUMERIC(10, 4),
    -- Both ends of the span, because every estimate reports the date range its
    -- evidence covers (PRD section 9).
    first_load_at     TIMESTAMPTZ,
    last_load_at      TIMESTAMPTZ,
    UNIQUE (broker_id, tier, origin_key, dest_key, equipment)
);

CREATE TABLE IF NOT EXISTS carrier_stats (
    id                BIGSERIAL PRIMARY KEY,
    broker_id         TEXT NOT NULL REFERENCES brokers (id),
    source_carrier_id TEXT NOT NULL,
    tier              TEXT NOT NULL
        CHECK (tier IN ('ZIP3', 'METRO', 'REGION', 'REGION_ANY')),
    lane_key          TEXT NOT NULL,
    equipment         TEXT NOT NULL
        CHECK (equipment IN ('DRY_VAN', 'REEFER', 'FLATBED', 'UNKNOWN', 'ANY')),
    load_count        INTEGER NOT NULL,
    on_time_count     INTEGER NOT NULL,
    -- The *answerable* denominator: loads with an on-time verdict, which is not
    -- every load. Stored beside the numerator so scoring cannot divide by
    -- load_count and count every in-transit load as a miss (TASKS.md R1).
    on_time_eligible_count INTEGER NOT NULL,
    avg_rate_per_mile NUMERIC(10, 4),
    first_load_at     TIMESTAMPTZ,
    last_load_at      TIMESTAMPTZ,
    UNIQUE (broker_id, source_carrier_id, tier, lane_key, equipment)
);

-- Both columns above arrived in Phase 4, after these tables existed. schema.sql
-- is applied on every start (D8), so a database created before then is brought
-- into step here rather than needing a hand-run migration; on a fresh database
-- the CREATE TABLEs already have them and these are no-ops. The DEFAULT is
-- dropped immediately so the end state matches the CREATE TABLE exactly.
ALTER TABLE loads ADD COLUMN IF NOT EXISTS delivered_on_time BOOLEAN;
ALTER TABLE carrier_stats
    ADD COLUMN IF NOT EXISTS on_time_eligible_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE carrier_stats ALTER COLUMN on_time_eligible_count DROP DEFAULT;

-- Ranking reads every carrier on one lane at the accepted tier.
CREATE INDEX IF NOT EXISTS carrier_stats_lane_idx
    ON carrier_stats (broker_id, tier, lane_key, equipment);

-- ---------------------------------------------------------------------------
-- Shared carrier pool: the opt-in, and the record of who asked
-- (Phase 11, DECISIONS.md D4/D17)
-- ---------------------------------------------------------------------------

-- A broker is in the pool iff it has a row here. No row is the default, so the
-- feature is off for everybody on a fresh database and stays off until someone
-- opts in explicitly (S1). Opting *out* deletes the row rather than flipping a
-- flag, so "not in" has exactly one representation.
CREATE TABLE IF NOT EXISTS pool_opt_in (
    broker_id   TEXT PRIMARY KEY REFERENCES brokers (id),
    opted_in_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- D17's "every pool read is written to an audit row keyed by (broker_id,
-- source_load_id, asked_at)". The pool is not a directory — it answers only for
-- one of the requester's own ACTIVE loads — so this is what makes *repetition*
-- visible after the fact, which is the only defence against an opted-in broker
-- who asks honestly-shaped questions over and over and does arithmetic.
--
-- Append-only for the same reason sync_events is: carrier_pool_app is granted
-- SELECT and INSERT and nothing else, so there is no privilege with which to
-- edit away a record of having asked.
CREATE TABLE IF NOT EXISTS pool_audit (
    id             BIGSERIAL   PRIMARY KEY,
    broker_id      TEXT        NOT NULL REFERENCES brokers (id),
    source_load_id TEXT        NOT NULL,
    asked_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pool_audit_asked_idx
    ON pool_audit (broker_id, source_load_id, asked_at);

-- ---------------------------------------------------------------------------
-- Tenant isolation, enforced by the database (CLAUDE.md invariant 1)
-- ---------------------------------------------------------------------------

-- The role the application connects as, and the role every repository query
-- runs as. NOSUPERUSER/NOBYPASSRLS is the whole point: a superuser silently
-- ignores row-level security, so the guarantee only exists if the querying role
-- cannot.
--
-- It is a LOGIN role, and DATABASE_URL names it, so that the *ambient*
-- credential — the one a careless `psycopg.connect(DATABASE_URL)` picks up — is
-- the safe one. Owner rights live in a second credential (ADMIN_DATABASE_URL)
-- used only to apply this file. The password is no more secret than the one in
-- docker-compose.yaml; this separates privilege, not identity.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'carrier_pool_app') THEN
        CREATE ROLE carrier_pool_app;
    END IF;
END
$$;

-- Stated unconditionally rather than only at creation, so re-running this file
-- repairs a role that was loosened by hand, and so the attribute list is
-- declarative rather than historical.
ALTER ROLE carrier_pool_app
    WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT
    PASSWORD 'carrier_pool_app';

-- The bootstrap user must be a member to SET ROLE to it (broker_session works
-- on an admin connection too, which is what confines maintenance scripts).
GRANT carrier_pool_app TO CURRENT_USER;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO carrier_pool_app', current_database());
END
$$;

GRANT USAGE ON SCHEMA public TO carrier_pool_app;

-- Declarative, so tightening this list later actually tightens it.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM carrier_pool_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM carrier_pool_app;

GRANT SELECT ON brokers TO carrier_pool_app;
-- Append-only: no UPDATE, no DELETE, no privilege to grant itself either.
GRANT SELECT, INSERT ON sync_files, sync_events TO carrier_pool_app;
-- Current truth is upserted; nothing in the design deletes a load or a party.
GRANT SELECT, INSERT, UPDATE ON loads, carriers, customers TO carrier_pool_app;
-- Derived stats are deleted and re-inserted per dirty key — that is the rebuild.
GRANT SELECT, INSERT, UPDATE, DELETE ON lane_stats, carrier_stats TO carrier_pool_app;
-- Joining and leaving the pool. No UPDATE: the row has nothing to change, and
-- leaving is a DELETE so that "not in the pool" has one representation.
GRANT SELECT, INSERT, DELETE ON pool_opt_in TO carrier_pool_app;
-- Append-only, like the sync log.
GRANT SELECT, INSERT ON pool_audit TO carrier_pool_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO carrier_pool_app;

-- The bound broker, or a loud failure. Reading the setting directly is not
-- enough: an unset custom GUC raises, but one whose transaction-local value has
-- already expired reads back as '' and would quietly match no rows. Both are
-- fail-closed, but "you forgot the binding" and "this broker has no data" must
-- not look the same to whoever is debugging.
CREATE OR REPLACE FUNCTION current_broker() RETURNS TEXT
LANGUAGE plpgsql STABLE PARALLEL SAFE AS $$
DECLARE
    bound TEXT := current_setting('app.broker_id', true);
BEGIN
    IF bound IS NULL OR bound = '' THEN
        RAISE EXCEPTION 'no broker bound: tenant tables are reachable only through '
            'BrokerRepository / broker_session (CLAUDE.md invariant 1)'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN bound;
END
$$;

-- One policy per tenant table, all identical: you see your bound broker's rows,
-- and you may only write rows carrying that broker.
DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'sync_files', 'sync_events', 'loads', 'carriers', 'customers',
        'lane_stats', 'carrier_stats', 'pool_opt_in', 'pool_audit'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS broker_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY broker_isolation ON %I'
            ' USING (broker_id = current_broker())'
            ' WITH CHECK (broker_id = current_broker())', t);
    END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- The one cross-broker reader (Phase 11 S3, DECISIONS.md D17)
-- ---------------------------------------------------------------------------
--
-- Everything above this line is tenant-confined: RLS is forced on every table
-- carrying a broker_id, and an unbound read raises out of current_broker().
-- The shared pool is the single place where one broker's answer may be informed
-- by another's data, and D17 spends its length on making that place small.
--
-- It is a **projection, not a filter**. A filter over the full record is one
-- forgotten SELECT * away from publishing what Broker B pays a carrier — the
-- exact commercial harm D4 names. The view below has no rate column to forget,
-- and three separate things have to be defeated to add one:
--
--   1. The view's column list, which names every column that crosses.
--   2. carrier_pool_reader's *column-level* SELECT grants, which do not include
--      carrier_stats.avg_rate_per_mile and include nothing at all on `loads`,
--      `lane_stats`, `customers`, `sync_files` or `sync_events`. So editing the
--      view to select a rate does not leak a rate — it fails to create, with
--      "permission denied for table carrier_stats". The barrier survives
--      somebody editing the SQL, which is the point of putting it in the
--      catalog rather than in Python.
--   3. app.domain.pool.PoolCarrier, a dataclass with no money attribute, which
--      is the only type the pool read path returns.
--
-- Raw counts do not cross either: the view emits *bands*. Suppression below 5
-- loads is the WHERE clause, and 5-9 / 10-19 / 20-49 / 50+ is the CASE, so the
-- integer never leaves the database and no caller can un-bucket what it was
-- never given. D17 is explicit that this is obfuscation and not a privacy
-- proof: at three brokers k-anonymity is arithmetically unavailable, because
-- every pooled statistic about a shared carrier is one other broker's data
-- minus your own. Bands are why a count crosses as a range rather than as a
-- number a competitor can subtract from.

-- The owner of the view, and the only role in the system that sees across
-- brokers. NOLOGIN and no password: nothing can connect as it, and
-- carrier_pool_app is deliberately *not* a member, so the only way to exercise
-- this role's reach is through the view it owns.
--
-- BYPASSRLS is required and is the riskiest line in the feature. schema.sql
-- declares FORCE ROW LEVEL SECURITY on every tenant table, so even the table
-- owner is subject to broker_isolation and an unbound read raises out of
-- current_broker(); a view owner without the bypass could therefore never
-- assemble a cross-broker row at all. The mitigation is not the grant, it is
-- the projection: this role can reach carriers and carrier_stats across every
-- broker and still cannot name a rate, because it was never granted the column.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'carrier_pool_reader') THEN
        CREATE ROLE carrier_pool_reader;
    END IF;
END
$$;

-- Declarative, like carrier_pool_app's: re-running this file repairs a role
-- that was loosened by hand.
ALTER ROLE carrier_pool_reader
    WITH NOLOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;

-- The bootstrap user must be a member to hand it ownership of the view.
GRANT carrier_pool_reader TO CURRENT_USER;
GRANT USAGE ON SCHEMA public TO carrier_pool_reader;

-- Table-level first, then the column lists. REVOKE ALL on a table does not
-- remove column-level privileges, so the forbidden columns are named again
-- below rather than assumed gone.
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM carrier_pool_reader;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM carrier_pool_reader;

-- What the view is allowed to read. Column-level SELECT, so the grant is the
-- same list as D17's "what crosses" table and can be diffed against it.
GRANT SELECT (broker_id, source_carrier_id, name, mc_number, dot_number, phone,
              home_city, home_state)
    ON carriers TO carrier_pool_reader;
GRANT SELECT (broker_id, source_carrier_id, tier, lane_key, equipment,
              load_count, on_time_count, on_time_eligible_count, last_load_at)
    ON carrier_stats TO carrier_pool_reader;
GRANT SELECT (broker_id) ON pool_opt_in TO carrier_pool_reader;

-- Named explicitly, so that reading this file tells you what was withheld and
-- so a hand-granted column is taken back on the next start (D8). These are
-- D17's "never crosses" rows that live on a table the view does touch; the
-- tables it does not touch at all get nothing by the REVOKE above.
REVOKE ALL (avg_rate_per_mile) ON carrier_stats FROM carrier_pool_reader;
REVOKE ALL (last_delivery_lat, last_delivery_lon, last_delivery_at)
    ON carriers FROM carrier_pool_reader;

-- Dropped and recreated rather than CREATE OR REPLACEd: replacing a view
-- refuses any change to the column list, which would make tightening this
-- projection require a hand-run migration. The grant below is re-issued on
-- every start for the same reason (D8).
DROP VIEW IF EXISTS pool_carrier_lane;

-- One row per (contributing broker, carrier, lane, equipment pool). Every
-- column is either the carrier's own published identity or a band.
--
-- security_invoker is left at its default of false, so the underlying tables
-- are read as the *owner* — which is what lets carrier_pool_reader's BYPASSRLS
-- apply and what makes its column grants the binding constraint. A caller who
-- joins this view back to `loads` inside a broker_session still gets only its
-- own rows: the policy filters the join, not the projection.
--
-- current_broker() in the WHERE clause below is evaluated against the
-- *invoker's* session setting, not the owner's, because app.broker_id is a
-- transaction-local GUC rather than anything the owner carries. So the view is
-- owner-privileged for reach and invoker-scoped for permission, which is
-- exactly the split this feature needs.
CREATE VIEW pool_carrier_lane AS
SELECT
    -- Not part of what crosses: the read path counts distinct contributors
    -- with it, and app.domain.pool.PoolCarrier has no field it could be copied
    -- into. Self-exclusion does not depend on a caller using it — see the
    -- WHERE clause.
    s.broker_id                                   AS contributor_broker_id,
    -- The cross-TMS identity key (D2). Trimmed and upper-cased here so the
    -- match is done once, in the projection, rather than by every caller.
    upper(btrim(c.mc_number))                     AS mc_number,
    c.dot_number                                  AS dot_number,
    c.name                                        AS name,
    c.phone                                       AS phone,
    c.home_city                                   AS home_city,
    c.home_state                                  AS home_state,
    s.tier                                        AS tier,
    s.lane_key                                    AS lane_key,
    s.equipment                                   AS equipment,
    -- Depth of the relationship, as a band. The floor of 5 is CLAUDE.md's
    -- minimum sample reused: a carrier below it is not in this view at all, so
    -- there is no row to un-bucket.
    CASE
        WHEN s.load_count >= 50 THEN '50+'
        WHEN s.load_count >= 20 THEN '20-49'
        WHEN s.load_count >= 10 THEN '10-19'
        ELSE '5-9'
    END                                           AS load_band,
    -- The same band as an ordinal, because '5-9' > '50+' lexicographically and
    -- the read path aggregates several contributors by taking the strongest.
    CASE
        WHEN s.load_count >= 50 THEN 4
        WHEN s.load_count >= 20 THEN 3
        WHEN s.load_count >= 10 THEN 2
        ELSE 1
    END                                           AS load_band_rank,
    -- Reliability without the raw pair: D16 shows that "18 of 22" is a
    -- fingerprint identifying one carrier under one broker. NULL is a real
    -- third state — nothing on this lane has delivered yet — and is not 0%.
    CASE
        WHEN s.on_time_eligible_count = 0 THEN NULL
        WHEN s.on_time_count::numeric / s.on_time_eligible_count >= 0.90 THEN '90+'
        WHEN s.on_time_count::numeric / s.on_time_eligible_count >= 0.75 THEN '75-89'
        ELSE '<75'
    END                                           AS on_time_band,
    CASE
        WHEN s.on_time_eligible_count = 0 THEN NULL
        WHEN s.on_time_count::numeric / s.on_time_eligible_count >= 0.90 THEN 1
        WHEN s.on_time_count::numeric / s.on_time_eligible_count >= 0.75 THEN 2
        ELSE 3
    END                                           AS on_time_band_rank,
    -- "Still running", with no date attached. Measured against the freshest
    -- delivery the pool holds rather than against now(), for the same reason
    -- BrokerRepository.latest_sync_at exists: an answer stays reproducible from
    -- the fixture instead of decaying as the wall clock walks away from day 11.
    -- Keep the 30 in step with app.domain.pool.POOL_RECENCY_DAYS.
    COALESCE(
        s.last_load_at > (
            SELECT max(cs.last_load_at)
            FROM carrier_stats cs
            JOIN pool_opt_in po ON po.broker_id = cs.broker_id
        ) - INTERVAL '30 days',
        FALSE
    )                                             AS active_recently
FROM carrier_stats s
JOIN carriers c
    ON c.broker_id = s.broker_id
   AND c.source_carrier_id = s.source_carrier_id
-- Opt-in, enforced in the projection itself. A broker that leaves disappears
-- from the pool on its next read; there is no copy of its rows to expire.
JOIN pool_opt_in p ON p.broker_id = s.broker_id
-- Three conditions on the *reader*, in the projection rather than in the query
-- that reads it, so that none of them is something a call site could forget.
--
--   * current_broker() raises when no broker is bound, so this view fails
--     closed outside a broker_session exactly like every tenant table does. A
--     careless psycopg.connect(DATABASE_URL) followed by SELECT * FROM
--     pool_carrier_lane gets the same "no broker bound" error it would get from
--     `loads`, rather than the whole pool.
--   * The pool is reciprocal: you see it only if you are in it. Enforced here,
--     so a broker that never opted in cannot read one banded row even through
--     hand-written SQL.
--   * A broker never sees its own contribution back. Its own carriers are in
--     its ranking already, scored from real numbers rather than from bands.
WHERE EXISTS (
        SELECT 1 FROM pool_opt_in me WHERE me.broker_id = current_broker()
      )
  AND s.broker_id <> current_broker()
  AND c.mc_number IS NOT NULL
  AND btrim(c.mc_number) <> ''
  -- METRO and REGION only. A ZIP3 pair is roughly a facility, and naming the
  -- facility a competitor's carrier runs into is naming their shipper.
  -- REGION_ANY is excluded because its lane key is the region with no
  -- equipment dimension, which the read path already reaches via REGION.
  AND s.tier IN ('METRO', 'REGION')
  -- Suppression below the minimum sample, applied before anything is banded.
  AND s.load_count >= 5;

ALTER VIEW pool_carrier_lane OWNER TO carrier_pool_reader;

-- The application role gets SELECT on the view and nothing else new. It has no
-- membership in carrier_pool_reader and no privilege on another broker's rows;
-- this one projection is the entire widening.
GRANT SELECT ON pool_carrier_lane TO carrier_pool_app;

-- pool_audit's sequence was created after the blanket sequence grant above, so
-- it is granted again here: on a fresh database the earlier statement ran
-- before the table existed.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO carrier_pool_app;
