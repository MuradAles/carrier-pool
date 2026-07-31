## How to run this

```bash
docker compose up          # then open http://localhost:5173
```

That is the whole sequence from a clean checkout. Nothing else to install, no API keys, no network at runtime. The backend applies its schema and ingests all 132 sync files in chronological order before Uvicorn accepts a connection, so the UI is never up against an empty database. About 11 seconds to the first response on an M4, of which roughly 4 is this project's own work.

The fastest way to see it working without the UI:

```bash
cd backend && ./.venv/bin/python -m scripts.e2e_check
```

It builds a throwaway database, ingests all 132 files, and asserts four named day-11 loads (one per lane tier) against figures computed before the code existed.

| Document | What is in it |
|---|---|
| [`DECISIONS.md`](DECISIONS.md) | The judgment calls and the alternatives rejected, what I would do next, and the honest limits |
| [`RUNNING.md`](RUNNING.md) | Every command, executed rather than assumed: timings, ports, the credential split, how to run the tests, how to regenerate the fixtures, and a troubleshooting table |
| [`data/TRACEABILITY.md`](data/TRACEABILITY.md) | Per day-11 load: the behaviour it proves, its supporting history, and the expected answer. Computed from the fixtures before any of this code was written |

Start at load `127412794` under broker FreightFlow. `data/TRACEABILITY.md` holds the arithmetic behind its answer, so every figure on that screen can be checked against the raw sync files with a calculator.

---

# Take-Home: Carrier Recommendation for Freight Brokers

- You may use AI coding tools (Claude Code, Codex, Cursor, etc) are strongly encouraged.
- With AI tools and the provided skeleton, a working baseline is roughly a **4-hour job — that's the floor, not the goal**. Strong submissions typically take one to two focused days on top.
- **Cutting scope deliberately is a valid strategy, not a failure** — a smaller thing done deeply beats a big thing done shallow. Say what you cut and why in `DECISIONS.md`.
- We want to see how you think, how deep you go, and which problems you notice on your own.
- In the review call you'll walk us through your decisions and defend them.

## The world

A **freight broker** is a middleman:

- **Customers (shippers)** — companies that have goods to move.
- **Carriers** — trucking companies that move the goods.
- The customer pays the broker one amount (**customer rate**). The broker (ideally) pays the carrier a smaller amount (**carrier rate**). The broker keeps the difference (**margin**).

Each shipment is called a **load**: a pickup place, a delivery place, the truck type needed (dry van, refrigerated, flatbed, etc.), dates, and weight.

A load goes through statuses as it moves through real life:

| Status | Plain meaning |
|---|---|
| `PLANNED` | The customer asked the broker to move this load; nothing has happened yet |
| `ACTIVE` | The broker is now searching for a carrier to take it |
| `COVERED` | A carrier said yes and is booked; the price the broker will pay them is now fixed |
| `IN_TRANSIT` | The truck is on the road |
| `DELIVERED` | The goods arrived |
| `COMPLETED` | All paperwork is done and the final money amounts are confirmed |

Loads can be updated or corrected at any point — freight data is messy.

Two more concepts:

- A **lane** is a from→to pair (for example "Dallas area → Houston area"). A carrier that has done many loads on or near a lane is likely a good fit for the next load on it.
- But what counts as "the same lane" is tricky. Think of New York City and Newark, NJ: they are ~10 miles apart, so for a trucker, Chicago → NYC and Chicago → Newark are practically the same lane — yet they have different city names *and* different states, so grouping history by city or by state would treat them as unrelated. Going the other way, "Texas → Texas" as one lane is useless: Dallas → Houston is 240 miles, El Paso → Houston is 750. You will face the same issue at smaller scale inside the Texas Triangle (suburbs of one metro vs another).
- **Deadhead** = empty miles a truck drives to reach a pickup. Carriers hate it. A truck that just delivered close to your new load's pickup is an easy yes.

## The problem

The platform you are building serves **multiple freight brokers**:

- Each broker runs a **different TMS** (Transportation Management System — the software where all their loads, carriers, and customers live). So each broker's data arrives in a different shape.
- Every day, each broker gets new loads (and sometimes new customers and carriers) *and* updates to existing ones.

For a broker's `ACTIVE` load, your platform must answer two questions:

1. **Which of my carriers should I call first, and why?**
2. **What should I expect to pay a carrier for this load?**

Both answers must come from the broker's own historical data. The broker must be able to see *why* — a bare score or price with no explanation is not useful.

**Bonus — the shared carrier pool.** If you have the appetite: let brokers opt in to a shared carrier pool, so a load can also be matched with carriers known by *other* opted-in brokers. Sharing between competitors is sensitive — so if you attempt this, clearly define and indicate what data crosses the broker boundary (and what never does), and design the sharing around that.

## Repository layout (your starting point)

This repo is an empty shell — placeholder Dockerfiles, compose file, and frontend/backend stubs. You fill it in (or restructure it). The only thing that matters out of the box is `data/`.

```
README.md                       # this file
docker-compose.yaml             # empty shell — yours to fill
backend/                        # empty Dockerfile + pyproject.toml stub
frontend/                       # empty Dockerfile + Vite-style stub
data/
  tms_a_freightflow/            # one directory per TMS
    example_sync.jsonc          # commented schema example — READ THIS FIRST
    example_sync_next.jsonc     # the following sync: same load, updated (how changes arrive)
    2026-07-06T06-00_sync.json  # empty placeholder — shows the filename convention
  tms_b_hauldesk/
    example_sync.jsonc
    2026-07-06T06-00_sync.json
  tms_c_brokeros/
    example_sync.jsonc
    2026-07-06T06-00_sync.json
```

The `example_sync.jsonc` files are the schema documentation (comments included). The real sync files you generate are plain `.json`, named `{YYYY-MM-DD}T{HH-MM}_sync.json` (ISO-8601-style, so filenames sort chronologically).

## Constraints (the few we do impose)

**Starting point**

- Assume the data has already been downloaded from each TMS — the raw data sits in the `data/` directories, exactly as the TMS produced it. Don't build or fake the TMS APIs themselves.
- **We provide the 3 fictional TMS schemas** — see `data/tms_a_freightflow/`, `data/tms_b_hauldesk/`, `data/tms_c_brokeros/`. How you get from their raw shapes to answers is yours to design.
- Each TMS is synced **every 6 hours** (00:00, 06:00, 12:00, 18:00). Every sync produces one self-contained file in that TMS's directory, with the sync datetime in the filename. A sync contains **1–3 loads**: everything created or changed since the last sync.

**Data (synthetic — you generate it; AI is good at this, but you own its sanity)**

- Geography: loads move within the **Texas Triangle** (Dallas–Fort Worth, Houston, San Antonio areas). Spread stops across nearby towns and suburbs, not just the three city centers.
- Create the sync files for **10 simulated days** (4 syncs per TMS per day, following the provided schemas and examples). Use AI to write the files, but *direct* it — **design the data like test cases for your own system**, not random noise. Every behavior you want to show off should have data that demonstrates it.
- At minimum, the data must contain these scenarios (how many and when is up to you):
  1. Loads progressing through the **full lifecycle across syncs**, with money amounts appearing as they become known (e.g. the carrier rate gets fixed when a carrier is booked; final amounts confirmed at completion).
  2. **Corrections** — loads whose *already-recorded* amount or detail changes to a new value in a later sync.
  3. **Contrast**: lanes with rich history next to lanes with thin history; carriers with lots of experience next to carriers with almost none.
- **Day 11** brings fresh loads that are still looking for a carrier — the ones your system must answer for, using days 1–10 as history. We should be able to look at your data and trace *why* your system gave each day-11 answer.
- **Ingestion processes one sync file at a time, in chronological order** — like the real scheduled syncs would have. No loading everything in one shot.

**Platform**

- **Multi-tenant**: one broker's data must never leak into or influence another broker's answers — the bonus pool, if you build it, is the single deliberate opt-in exception.
- **Stack**: use whatever you want. We recommend Python/TypeScript backend + TypeScript/React (and Postgres via docker compose) because that's what the shell hints at — but the stubs are optional, not a mandate.
- **Frontend**: any working UI that shows a load list, and per load the price estimate plus the ranked carriers with their reasoning. Correctness and clarity count; visual polish counts for nothing.
- **How to run**: document it. We will run your project ourselves — a short doc (README section or similar) with the command sequence to bring everything up and reproduce your results. An end-to-end check that exercises that path is a plus — we care that you thought about it, not which tool you picked.

## What we're looking for

Not feature count. We read for the problems you noticed and how you resolved them, for example:

- What happens to your analytics when yesterday's load is corrected today? Do you patch the derived numbers, or rebuild them from scratch — and what would break at millions of loads?
- What is a "lane", exactly, when pickups are scattered across suburbs?
- How does a scoring formula stay fair to a carrier with little history?
- Where should a price estimate come from when the exact lane has little data?
- (If you attempt the pool) what exactly is shared, and how do you prove nothing else leaks?

Some of these have no single right answer — your reasoning is the deliverable as much as the code.

Include a short `DECISIONS.md`:

- The judgment calls you made and the alternatives you rejected.
- What you'd do next with more time.
- Honest limitations score better than hidden ones.
