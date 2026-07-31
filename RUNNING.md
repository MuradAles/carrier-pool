# Running it

Every command below was executed on 2026-07-30, and every block of output is pasted, not
reconstructed. The whole sequence was run twice from a **destroyed database volume** —
see [§1](#1-bring-it-up) for the per-phase timings and
[the rehearsal](#the-clean-start-rehearsal) for what was and was not measured.

Prerequisites: Docker with Compose v2. Nothing else. No API keys, no network at runtime
(`CLAUDE.md` invariant 7): geography is a hardcoded table and distance is Haversine × 1.2.

**In a hurry?** `docker compose up`, then open http://localhost:5173. **Give the first
boot several minutes** — most of it is Postgres initialising an empty data directory, not
this application; §1 says how to watch it so you can tell the difference between slow and
hung. If you would rather see the system reason before you look at a screen,
[§2](#2-the-fastest-way-to-see-it-work) is one command that prints a whole answer with its
evidence.

---

## 1. Bring it up

```
docker compose up
```

Three services: `db` (postgres:16-alpine), `backend` (FastAPI on :8000), `frontend`
(Vite dev server on :5173). Compose waits for the database healthcheck before starting
the backend.

**The backend does not serve until the data is in.** Its FastAPI lifespan applies
`schema.sql`, creates the application role, and ingests all 132 sync files
chronologically — synchronously, before the first request is answered
(`DECISIONS.md` D8).

### How long the first boot takes

**Set your expectations before you start it: the first `docker compose up` can take
minutes, and almost none of that is this application.** A warm restart proves nothing —
`ingest_all` is idempotent and the lifespan runs it on every boot, so a restart against a
populated database skips all 132 files. Only an empty volume exercises Postgres `initdb`,
the schema, the role creation and the first real ingest. So that is what was measured:
`docker compose down -v`, then up from nothing.

Phase by phase, from the container log timestamps of one such run (Apple M4, 24 GB,
macOS 26.5.2, Docker 29.5.3):

| phase | elapsed | what it is |
|---|---|---|
| container + network create, Postgres `initdb` | 0.6s | `db` writes its data directory from scratch |
| healthcheck interval before compose starts `backend` | ~5s | `interval: 5s` in `docker-compose.yaml`; the first probe is the gate |
| uvicorn boot | 0.5s | |
| **schema bootstrap + role creation + 132-file ingest** | **4.3s** | **this is the only part that is our code** |
| | **11.4s total** | `docker compose up -d` to the first HTTP 200 |

**That total is this machine, and yours will differ — possibly by a lot.** The same
sequence run by a colleague on different hardware took **264 seconds** to the first HTTP
200. Their application phase was 4.0s, within a rounding error of the 4.3s above. Every
one of the remaining ~4.3 minutes was container creation, `initdb` and healthcheck
retries, i.e. Docker and Postgres, not us.

The useful way to read that: **the application's own work is about four seconds and is
stable; everything around it is a property of your machine and can be two orders of
magnitude larger.** If your first boot takes four minutes, nothing is wrong.

The image build is a separate cost, paid once:

| build | time |
|---|---|
| `docker compose build --no-cache`, warm pip/npm caches | 11.4s (`pip install` 8.5s and `npm install` 5.8s, in parallel) |
| the same build with a **cold** pip cache | the `pip install` step alone reported 149.7s |

Base image pulls (`postgres:16-alpine` 411 MB, `python:3.12-slim` 205 MB,
`node:22-alpine`) are in none of these numbers — they were already cached locally. Budget
several minutes for a genuinely fresh machine's first build, seconds for every one after.

### Watch it, so the wait is legible

```
docker compose logs -f backend
```

This matters more than it sounds. The gap between "I typed `docker compose up`" and "the
UI answers" is the single most likely way this submission gets killed by someone who
assumes it has hung. The startup is instrumented so you can see it move:

```
INFO:     Waiting for application startup.
INFO:     startup: applying database schema
INFO:     startup: schema ready in 0.0s
INFO:     ingest: 132 sync files from 3 brokers under /data
INFO:     ingest: 25/132 files (25 ingested, 0 already present, 0.7s)
INFO:     ingest: 50/132 files (50 ingested, 0 already present, 1.6s)
INFO:     ingest: 75/132 files (75 ingested, 0 already present, 2.5s)
INFO:     ingest: 100/132 files (100 ingested, 0 already present, 3.4s)
INFO:     ingest: 125/132 files (125 ingested, 0 already present, 4.2s)
INFO:     ingest complete: 132 files in 4.3s; 132 files ingested, 0 already present;
          1156 events, 316 load touches, 1556 lane-key rebuilds, 261 carrier position rebuilds
INFO:     startup complete in 4.3s
INFO:     Application startup complete.
```

Two different silences, and it is worth knowing which one you are in:

- **Nothing from `backend` at all yet** — you are still in Postgres `initdb` and the
  healthcheck. This is the phase that can run for minutes on a slow disk. Check
  `docker compose logs -f db` instead; it narrates its own initialisation.
- **`Waiting for application startup.` and then nothing** — you are in the ingest, and it
  reports every 25 files. If those lines are appearing, it is working.

On a second boot the same lines read `132 already present` and the whole thing takes a
fraction of a second, because the volume persists and ingestion is idempotent.

### There is no empty-UI window

The first request that succeeds after a cold start already has everything:

```
$ curl -s ".../api/loads?broker_id=broker_a&status=ACTIVE" | ...
ACTIVE loads: 5 ['127412794', '127412960', '127413097', '127413245', '127413326']
```

That is not luck. Uvicorn does not accept connections until the lifespan coroutine
returns, and the ingest runs inside it, so *any* response you can get is a post-ingest
response.

## 2. The fastest way to see it work

Before the UI, if you want to see the reasoning rather than a screenshot of it:

```
cd backend && ./.venv/bin/python -m scripts.e2e_check
```

It builds its own throwaway database (never `carrier_pool`, so it collides with nothing),
applies the schema, ingests all 132 files in order, and asks the same domain functions the
API routes call for four named day-11 loads — one per tier rung. It prints the tier walk,
the winning carrier with its reasons, and the price with its range, then asserts every one
of them:

```
H1 end-to-end check -- fresh database, 132 files, 4 named day-11 loads (one per tier rung)

== Ingestion (132 ingested, 0 skipped) ==
  295 loads across 3 brokers (broker_a 98, broker_b 98, broker_c 99)
  sum(carrier_rate)  = $150,716.43 (expected $150,716.43)
  sum(customer_rate) = $186,840.92 (expected $186,840.92)
  idempotency re-run: 0 ingested, 132 skipped (sums unchanged, re-verified above)

== DAY11-RICH -- broker_a / 127412794 ==
  tier walk: ZIP3 12 (ACCEPTED)
  accepted tier: ZIP3, 12 loads back the estimate
  top carrier: IBRAHIM TRANSPORT INC (score 84.3)
    - Ran 8 loads on 750->774, dry van at the ZIP3 tier
    - Last load on this lane 3 days ago (2026-07-13)
    - Has hauled dry van for you (20 loads)
    - Delivered in IRVING, TX 75061 yesterday, 11.6 mi from your pickup
    - 100% on-time on this lane (8 of 8 delivered loads), shrunk to 97% toward the lane's 92%
    - Averages $1.78/mi on 750->774, dry van
  price estimate: $526.88 (range $518.00 - $534.28), confidence medium
  provenance: 'median of 12 loads on 750->774, dry van, 2026-07-06 to 2026-07-13 — medium confidence'
  PASS

[three more blocks in the same shape: DAY11-UNKNOWN-EQUIP (METRO, capped to medium),
 DAY11-THIN (REGION, low), DAY11-COLDSTART (REGION_ANY, low)]

ALL CHECKS PASSED in 10.6s
```

The money sums in the ingestion block are the load-bearing assertion there: a
double-counted TMS B rate line would show up in them and essentially nowhere else.
`WALKTHROUGH.md` derives the `DAY11-RICH` block above by hand from the raw JSON, without
running anything.

Same code also runs as a test: `./.venv/bin/python -m pytest tests/integration/test_end_to_end.py -q -s`.

## 3. Check it

```
$ curl localhost:8000/api/health
{"status":"ok","database":"ok","data_dir":"/data (132 sync files)"}
```

`132 sync files` is the number of files the container can see under `/data`. If it says
anything else, the `./data:/data:ro` mount is wrong and every answer below will be wrong
with it.

Then open **http://localhost:5173**. The Vite dev server proxies `/api/*` to
`http://backend:8000` (it resolves the compose service name at config time and falls back
to `localhost:8000` for a bare `npm run dev` — `frontend/vite.config.ts`). Every route the
UI uses, through the proxy:

```
$ B=http://localhost:5173/api
$ for u in "$B/brokers" \
           "$B/loads?broker_id=broker_a&status=ACTIVE" \
           "$B/loads/127412794?broker_id=broker_a" \
           "$B/loads/127412794/recommendations?broker_id=broker_a" \
           "$B/loads/127412794/price-estimate?broker_id=broker_a" \
           "$B/loads/127412794/recommendations?broker_id=broker_b" ; do
      curl -s -o /dev/null -w "%{http_code}\n" "$u"
  done
200      # brokers
200      # load list, day-11 ACTIVE
200      # load detail
200      # ranked carriers
200      # price estimate
404      # the same load, asked for as broker_b
```

The last line is the interesting one: `127412794` is broker_a's load, so broker_b asking
for it gets a 404, not broker_a's answer and not a 500.

---

## 4. What to look at

Pick a broker in the dropdown, then a day-11 `ACTIVE` load. Each of the 16 was planted to
demonstrate one behaviour. This table came out of the running API, not out of a document:

| broker | load | demonstrates | tier walk (rung:count) | conf. | estimate | top carrier |
|---|---|---|---|---|---|---|
| a | `127412794` | rich lane, narrowest tier fires | `ZIP3:12` | medium | $526.88 | IBRAHIM TRANSPORT INC · 84.3 |
| a | `127412960` | suburb scatter — metro beats city pairs | `ZIP3:3 → METRO:8` | medium | $591.14 | LONE STAR COLD LINES · 81.8 |
| a | `127413097` | thin lane, falls through to REGION | `ZIP3:2 → METRO:2 → REGION:70` | low | $351.94 | RIO GRANDE HAULING · 85.9 |
| a | `127413245` | cold-start shrinkage on a sparse lane | `ZIP3:2 → METRO:3 → REGION:7` | low | $475.89 | RIO GRANDE HAULING · 58.7 |
| a | `127413326` | deadhead alone decides the winner | `ZIP3:6` | medium | $360.00 | PINEY WOODS CARTAGE · 68.4 |
| b | `HD-2026-005053` | rich lane | `ZIP3:12` | medium | $601.99 | NORTH TEXAS LINE HAUL · 84.6 |
| b | `HD-2026-005063` | suburb scatter | `ZIP3:3 → METRO:8` | medium | $669.78 | DELTA PRIME · 76.2 |
| b | `HD-2026-005066` | thin lane | `ZIP3:2 → METRO:2 → REGION:71` | low | $424.38 | MISSION VALLEY TRUCKING · 88.9 |
| b | `HD-2026-005077` | rare equipment drives the walk to rung 4 | `ZIP3:1 → METRO:2 → REGION:4 → REGION_ANY:93` | low | $502.05 | NORTH TEXAS LINE HAUL · 77.7 |
| b | `HD-2026-005084` | deadhead | `ZIP3:6` | medium | $411.71 | WOODLANDS REGIONAL · 69.5 |
| c | `a0jO900000Pe4PRTiG` | rich lane | `ZIP3:12` | medium | $786.38 | Metroplex Ridge Logistics · 83.1 |
| c | `a0jO900000ktYClT5a` | suburb scatter | `ZIP3:3 → METRO:8` | medium | $781.24 | Bay Area Cold Carriers · 76.2 |
| c | `a0jO900000r8rMVzYT` | thin lane | `ZIP3:2 → METRO:2 → REGION:68` | low | $488.59 | Espinoza Brothers Trucking · 84.0 |
| c | `a0jO900000ZlapADYm` | cold start | `ZIP3:2 → METRO:3 → REGION:7` | low | $638.40 | Espinoza Brothers Trucking · 54.0 |
| c | `a0jO900000Lzwi4yce` | deadhead | `ZIP3:6` | medium | $469.26 | Montgomery County Haulers · 68.1 |
| c | `a0jO900000RE5kFMEU` | **UNKNOWN equipment** skips the filter | `ZIP3:2 → METRO:31` | medium | $742.30 | Metroplex Ridge Logistics · 80.6 |

Four things worth clicking on specifically:

**Tenant isolation, visible in three clicks.** The `DAY11-RICH` load of each broker lands
on the *identical* lane key `750→774` with the *identical* count of 12, and gets a
different answer:

```
broker_a  750->774  n=12  p25/p50/p75 1.75/1.78/1.805     296.0 mi -> $526.88
broker_b  750->774  n=12  p25/p50/p75 2.0925/2.16/2.1775  278.7 mi -> $601.99
broker_c  750->774  n=12  p25/p50/p75 2.48/2.51/2.52      313.3 mi -> $786.38
```

Each broker's fixture sits in a deliberately distinct rate band (`DECISIONS.md` D13), so a
leak is not a subtle statistical shift — it moves the money. Pooled, those 36 loads give a
median of 2.16. If any of the three ever prints 2.16 over n=36, the boundary is broken.

**A correction, as it arrived.** Open broker_b's `HD-2026-004733` and read the Sync history
panel: four rate lines on 2026-07-11, then a single `-120.00 ADJUSTMENT` on 2026-07-12 that
arrived in a file whose `loads` array does not mention this load at all. `674.70 + 148.10 −
120.00 = 702.80`, and the derived stats were rebuilt, not patched. `WALKTHROUGH.md` §6
traces it.

**Confidence that is labeled, not hidden.** `127413097` accepts at `REGION` with 70 loads
and still says **low**, because rung 3 is low by rule (`DECISIONS.md` D6) no matter how
many loads back it. `a0jO900000RE5kFMEU` has 31 loads — high on count alone — and is capped
to **medium** because the pool is 23 dry van + 8 reefer (D15). Both name the reason in the
provenance line.

**A zero-ish carrier is still on the list.** Every load returns *all* the broker's carriers,
worst last, with the reason they are weak — e.g. *"Delivered in PEARLAND, TX 77584 9 days
ago, 304.8 mi from your pickup — past the 250 mi cutoff, so no proximity credit."* The UI
does not truncate to a top-N.

---

## 5. The credentials split — do not "simplify" it

`docker-compose.yaml` passes the backend two database URLs:

```yaml
DATABASE_URL:       postgresql://carrier_pool_app:carrier_pool_app@db:5432/carrier_pool
ADMIN_DATABASE_URL: postgresql://carrier:carrier@db:5432/carrier_pool
```

They are not redundant. `carrier_pool_app` is created by the schema bootstrap with
**NOSUPERUSER, NOBYPASSRLS**, and RLS is enabled *and forced* on all seven tenant tables.
Both facts, straight out of the freshly bootstrapped database from §1's clean start:

```
$ docker compose exec db psql -U carrier -d carrier_pool \
    -c "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles
        WHERE rolname IN ('carrier','carrier_pool_app');"
     rolname      | rolsuper | rolbypassrls
------------------+----------+--------------
 carrier          | t        | t
 carrier_pool_app | f        | f

$ ... -c "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class c
          JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='public' AND c.relkind='r' ORDER BY relname;"
    relname    | relrowsecurity | relforcerowsecurity
---------------+----------------+---------------------
 brokers       | f              | f
 carrier_stats | t              | t
 carriers      | t              | t
 customers     | t              | t
 lane_stats    | t              | t
 loads         | t              | t
 sync_events   | t              | t
 sync_files    | t              | t
```

`brokers` is deliberately outside the fence: the list of brokers is the tenant directory,
not tenant data. The policies on the other seven key off a per-connection `app.broker_id`
setting that only the repository layer sets, so on the app role a query issued without a
broker bound does not return the wrong rows — it raises
`InsufficientPrivilege: no broker bound`. That is what makes `CLAUDE.md` invariant 1
structural rather than a convention every future call site has to remember.

`ADMIN_DATABASE_URL` names the table owner, and RLS does not apply to a table's owner. It
exists for exactly one job — applying `schema.sql` at startup — and `app/repository/db.py`
exposes it through a separate `connect_admin()` so its use is greppable.

Collapsing these to one URL, or granting `carrier_pool_app` BYPASSRLS to "make the tests
easier", removes the enforcement entirely while every test still passes. The 17
tenant-isolation tests were verified non-vacuous by mutating the RLS predicate to
`USING (true)`: 6 of 17 fail. They fail because the barrier is real, not because they
assert it exists.

---

## 6. Tests

### Stop the backend container first. This is not optional.

```
docker compose stop backend        # leave db running — the suite needs it
cd backend && ./.venv/bin/python -m pytest tests -q
docker compose start backend       # afterwards; it re-ingests on the way up
```

The `db` service must stay up — the suite reaches Postgres at `localhost:5432`, which is
where compose publishes it. The **backend** must not.

**Why, and what you see if you skip it.** The integration suite `TRUNCATE`s the seven
tenant tables of the shared `carrier_pool` database around every test. A backend that is
serving requests is reading those same tables, and the two deadlock:

```
$ ./.venv/bin/python -m pytest tests -q          # stack up, UI open in a browser
10 failed, 410 passed, 36 errors in 93.81s

E   psycopg.errors.DeadlockDetected: deadlock detected
E   DETAIL:  Process 6180 waits for AccessExclusiveLock on relation 251774 of database
E            16384; blocked by process 6342.
E            Process 6342 waits for RowExclusiveLock on relation 251791; blocked by 6180.
```

This is the ordinary reviewer path — `docker compose up`, look at the UI, run the tests —
so it is worth being precise about the trigger. **An idle backend is usually fine; a
backend answering requests is not.** With the stack up and nothing touching the API, three
consecutive full runs passed 454. With the UI being polled during the run, the same
command produced the failures above, and `pytest tests/integration -q` alone under the
same load gave `16 failed, 11 passed, 35 errors`. So "it passed for me once with the stack
up" is not evidence the combination is safe — it means nothing was talking to the API.

**And it can cost you the backend, not just the data.** If the backend happens to *boot*
while the suite is running — a `--reload` restart after an edit, or a `docker compose
restart` — the lifespan loses the same race and the process exits:

```
$ ./.venv/bin/python -m pytest tests/integration -q &   # then restart the backend
psycopg.errors.DeadlockDetected: deadlock detected
DETAIL:  Process 10412 waits for AccessExclusiveLock on relation 251774 of database
         16384; blocked by process 10397.
CONTEXT: SQL statement "ALTER TABLE sync_files ENABLE ROW LEVEL SECURITY"
ERROR:    Application startup failed. Exiting.

$ curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/api/health
000        # nothing listening
```

The suite itself passed `57 passed` in that run — so the only visible symptom is that the
API stopped answering. The same collision has also been seen killing startup inside the
ingest rather than the schema step, on a `lane_stats` `UniqueViolation`. Either way,
`docker compose start backend` (or `restart`) with no test running brings it back and
re-ingests. Stopping the backend first avoids all of it.

### The count

On a quiet machine with the backend container stopped, run twice back to back:

```
454 passed, 1 warning in 54.07s
454 passed, 1 warning in 53.65s
```

Per suite, same conditions:

| suite | tests | time | needs Postgres |
|---|---|---|---|
| `tests/unit` | 287 | 0.10s | no |
| `tests/data_integrity` | 22 | 0.89s | no |
| `tests/integration` | 57 | 34.86s | yes |
| `tests/adversarial` | 88 | 16.45s | yes |

If you would rather build your own environment than use the checked-in `backend/.venv` —
verified from scratch in a throwaway venv with only the dependencies `pyproject.toml`
declares, no editable install needed (the conftest puts `backend/` on `sys.path`):

```
python3 -m venv .venv
./.venv/bin/pip install "fastapi>=0.115" "uvicorn[standard]>=0.32" "psycopg[binary]>=3.2" \
                        "pydantic>=2.9" "pytest>=8.3" "httpx>=0.27"
./.venv/bin/python -m pytest tests -q     # 454 passed in 53.00s
```

### The first run after the database changes underneath it is unreliable

**This is a real defect in the test suite, not a caveat, and it is not rare.** With the
backend stopped and nothing else running, the first invocation after the database has been
disturbed — the backend repopulating it, an earlier interrupted run — does not pass.
**Three attempts, three failures**, each one the first run after restarting and then
stopping the backend:

```
attempt 1:  14 failed, 440 passed
attempt 2:  430 passed, 24 errors      psycopg.errors.DeadlockDetected
attempt 3:    1 failed, 453 passed     psycopg.errors.InternalError_: tuple concurrently updated
```

Immediately re-running the identical command passed 454 every time — four consecutive
clean runs after attempt 1, two after attempt 3. A colleague measuring it differently
— three consecutive runs of `tests/integration` from a reset database rather than three
first-runs — got `33 passed, 24 errors`, then `57 passed`, then `57 passed`: **one in
three**, and the same shape. Their errors included a `ForeignKeyViolation` on
`sync_events_sync_file_id_fkey` alongside the deadlocks.

Both measurements say the same thing: it is the *first* run against a database whose state
just changed that races, and re-running clears it.

**The part that matters: it does not always look like an infrastructure error.** Among the
failures observed were plain wrong-value assertions —

```
E   AssertionError: assert [] == ['2026-07-06T...00_sync.json']
E   assert 0 == 3
E   assert None is not None
```

— and a colleague reproducing the same flakiness saw `assert 9 == 12` and a top-ranked
carrier of `IRON HORSE FLATBED CO` where `ALAMO CHILL TRANSPORT` was expected. A reviewer
reading that would reasonably conclude the ranking is broken. It is not; the suite raced
itself and read a half-truncated database.

**So: if the first run fails, run it again before believing it.** If the second run also
fails, that is a real result worth reporting. This is recorded as a known limitation in
`DECISIONS.md` — the fix is per-run database isolation for the integration suite, which
the adversarial suite already does and the integration suite does not.

### Three more things that will bite you

Distinct from the two problems above, and from each other.

**1. Do not run the tests inside the backend container — they will lie to you.** Running
them there is the obvious thing to try, and it reports a healthy-looking green:

```
$ docker compose exec backend python -m pytest tests/unit tests/integration tests/adversarial -q
344 passed, 88 skipped in 16.22s
    SKIPPED  no Postgres at localhost:5432: ... Connection refused
```

Those 88 skips are the **entire adversarial suite** — every tenant-leak attack, every
hostile input, every correction-chain attack. They are gated on reaching Postgres at
`localhost:5432`, and inside the container `localhost` is the container, so they skip
rather than fail. `tests/data_integrity` does not even collect there
(`ModuleNotFoundError: No module named 'generate_data'` — its conftest resolves
`backend/scripts` relative to the repo root, a layout the container does not have).

**Run the suite from the host**, where `localhost:5432` is the port compose publishes.
That is the invocation at the top of this section, and it is the one that gives 454.

**2. Run one pytest invocation at a time.** Two concurrent runs collide, and the cause is
not what it looks like. It is a **catalog race**, not the truncation in item 3: the
adversarial suite creates its own private database per session, and two sessions issuing
`CREATE DATABASE`/`CREATE ROLE` at once fight over the same shared system catalog rows.
Reproduced, integration and adversarial started together:

```
tests/integration:   57 passed in 36.21s
tests/adversarial:    3 passed, 85 errors in 9.26s
    E  psycopg.errors.InternalError_: tuple concurrently updated
```

All 85 are errors at `admin_conn` fixture setup — the session never starts. Nothing is
corrupted and no data is wrong; re-run it alone and it passes. Sequential invocations are
fine, which is how the per-suite table above was produced.

**3. Afterwards, the database is empty.** Even on a completely clean run, the integration
suite leaves the seven tenant tables truncated — that is the same truncation that causes
the deadlock above, seen from the other end. After a run:

```
$ curl -s ".../api/loads?broker_id=broker_a&status=ACTIVE" | ...
ACTIVE loads: 0
```

A blank UI, and the load-detail routes 404. That is the test suite having done its job,
not a defect. Refill it either way:

```
curl -X POST localhost:8000/api/admin/ingest      # same pipeline, no restart; 4.1s
docker compose restart backend                    # or let the lifespan do it; slower,
                                                  #   only because of the restart itself
```

The admin route is idempotent on `broker_id + sync_file`, so calling it twice reports
`132 files ingested, 0 already present` and then `0 ingested, 132 skipped`.

### The end-to-end check

Moved to [§2](#2-the-fastest-way-to-see-it-work) — it is the first thing worth running,
not a testing appendix. It creates its own throwaway database, so it is safe to run
against a live stack.


---

## 7. Regenerating the fixtures

The 132 sync files and `data/TRACEABILITY.md` are generated, not hand-written, from a
single seed. Regenerating somewhere harmless and diffing proves it:

```
$ ./.venv/bin/python scripts/generate_data.py --out /tmp/regen
wrote 132 sync files + TRACEABILITY.md under /tmp/regen
validation: PASS (0 errors)          # 0.11s

$ diff -r data /tmp/regen
Only in data/tms_a_freightflow: example_sync.jsonc      # the provided schema docs
Only in data/tms_a_freightflow: example_sync_next.jsonc
Only in data/tms_b_hauldesk: example_sync.jsonc
Only in data/tms_c_brokeros: example_sync.jsonc
```

No content differences: all 132 `_sync.json` files and `TRACEABILITY.md` come back
byte-identical. As a digest,

```
$ cd data && find . -name '*_sync.json' | sort | xargs cat | shasum -a 256
d000c514881c2f6e9276a3eebe3ffd9551e77c6b822b996287b72b124770e2c1
```

and the same command over `/tmp/regen` gives the same hash. (`TASKS.md` DG9 quotes
`8fcc1679…` from a different digest scheme; the one above is reproducible with the command
printed beside it.)

Auditing the committed files without rewriting anything:

```
$ ./.venv/bin/python scripts/validate_data.py --validate-only
validation: PASS (0 errors)
```

The validator reconciles the emitted bytes against the plan appearance by appearance —
miles, carrier rate, weight, equipment, status, stops, carrier identity, and TMS B's
running `pay` sum. It was itself verified by a corruption harness of 21 named mutations
plus a positive control (`tests/data_integrity`): with reconciliation disabled, 21 fail and
only the control passes.

Running `generate_data.py` **without** `--out` rewrites `data/` in place. Same seed, same
bytes, so it is a no-op — but there is no reason to do it.

---

## 8. If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `/api/health` reports fewer than 132 sync files | `./data:/data:ro` not mounted | check you ran compose from the repo root |
| UI lists no loads, health is `ok` | you ran the integration suite; the tenant tables are truncated | `curl -X POST localhost:8000/api/admin/ingest` |
| `psycopg.errors.DeadlockDetected` during pytest | the backend container is serving while the suite truncates | `docker compose stop backend`, then re-run — §6 |
| API stops answering entirely (`curl` gets nothing), `Application startup failed. Exiting.` in the logs | the backend booted while pytest was running against `carrier_pool` | `docker compose start backend` with no test running — §6 |
| pytest fails once, passes on an immediate re-run | the suite's first-run race after the database changed under it — a known defect | re-run before believing it; §6 |
| `psycopg.errors.InternalError_: tuple concurrently updated` | two pytest invocations at once | run them one at a time |
| 88 tests "skipped" | pytest run inside the container | run from the host |
| `up` seems to hang, no `backend` output at all | Postgres `initdb` on an empty volume — minutes on a slow disk | `docker compose logs -f db`; see §1 |
| `Waiting for application startup.` then a pause | the lifespan ingest (D8) | `docker compose logs -f backend`; a line every 25 files |

**Not observed here, so stated as reasoning rather than as pasted output:** a machine with
something already bound to 5432, 8000 or 5173 will fail at `docker compose up` when Docker
cannot publish the port — change the host side of the mapping in `docker-compose.yaml`.
And a stopped Docker daemon fails before any of this (`TASKS.md` F2 records hitting that
during Phase 0).

---

## The clean-start rehearsal

This document was rewritten *after* running its own sequence from a destroyed volume, not
before, and then re-run a second time once the startup logging landed. What happened:

```
docker compose down -v           1.2s   volume carrier-pool_db_data Removed
docker compose build --no-cache 11.4s   (warm package caches — see §1)
docker compose up -d             6.3s   to return
                                 5.1s   more to the first HTTP 200   => 11.4s total
```

Against that empty-volume stack: `psql \l` listed only `carrier_pool` — the Phase 5 and
e2e scratch databases died with the volume, as expected, and nothing depended on them; the
app role came back `rolsuper=f, rolbypassrls=f` with RLS forced on all seven tenant tables;
`127412794` returned ZIP3 / 12 loads / medium / **$526.88 ($518.00–$534.28)** with IBRAHIM
TRANSPORT INC at **84.3**; all six UI routes answered through the Vite proxy with the
cross-broker request 404-ing; `scripts.e2e_check` passed in 10.6s; the full suite came back
**454 passed**, left the tenant tables truncated exactly as §6 warns, and one
`POST /api/admin/ingest` (4.1s) restored all five day-11 loads.

The suite numbers in §6 were measured separately and deliberately: backend container
stopped, nothing polling the API, and after discarding the first run — which is the
condition §6 tells you to reproduce, and the only one under which the count is stable.

**Nothing needed a step that is not on this page.** That was the thing being tested — a
reviewer who has to improvise has found a gap — and the sequence above is the whole of it.

### On the boot number specifically

Two people measured the same `down -v` → `up` → first-200 sequence on different machines
and got **11.4s** and **264s**. Both are real. The application phase was 4.3s and 4.0s
respectively — agreement to within a rounding error — and the entire spread is Docker
container creation, Postgres `initdb` and healthcheck retries. §1 gives the per-phase
breakdown rather than a single figure, because a single figure from either machine would
mislead the other. If you time it yourself, the number worth comparing against ours is the
`startup complete in Xs` line, not the wall clock.

### What I did not measure

- **Base image pulls.** `postgres:16-alpine` (411 MB), `python:3.12-slim` (205 MB) and
  `node:22-alpine` were already in this machine's image cache and are shared with several
  other projects on it, so deleting them to time a pull was not mine to do. Your first
  `docker compose up` pays that download; every subsequent one does not.
- **A build with a cold pip/npm cache.** The 11.4s build in §1 is with warm package
  caches; the same build with a cold pip cache reported 149.7s for `pip install` alone.
- **`git clone` into a fresh directory.** Everything ran against the working tree, which
  is clean apart from this file. The volume was destroyed; the checkout was not re-made.

Every other number and every quoted line of output on this page was produced on
2026-07-30 and pasted, not reconstructed.
