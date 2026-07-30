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
        'lane_stats', 'carrier_stats'
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
