/**
 * U6 — every version of this load as it arrived, so a correction is visibly a
 * correction.
 *
 * `sync_events` is append-only (CLAUDE.md invariant 3): a late fix is a *new*
 * row, not an edit of an old one, and derived stats are rebuilt from the whole
 * log rather than patched. This panel is the visible half of that promise. If a
 * rate changed after we first recorded it, a rep has to be able to see the
 * change and what it changed from — otherwise the correction machinery is
 * invisible to exactly the person it exists to reassure.
 *
 * `raw_json` is printed verbatim, as its TMS stated it, not as a canonical
 * projection. Rendering the normalized form would hide the very difference the
 * panel exists to show, and each TMS's own vocabulary is what a rep would find
 * if they went and looked at the source file named beside it.
 *
 * `HD-2026-004733` is the demonstration case: four `pay` rate lines arriving in
 * `2026-07-11T00-00_sync.json`, then a fifth `ADJUSTMENT` of −120 arriving the
 * next day in a file whose `loads` array never mentions this load at all. The
 * `RATE_LINE` entity type is what makes that fifth event visible here.
 *
 * No fetch of its own: the history rides along on `/api/loads/{id}`, so the
 * detail screen's loading and error states already cover it.
 */

import { instant } from "./format";
import type { SyncEvent } from "./types";

export function SyncHistoryPanel({ events }: { events: SyncEvent[] }) {
  return (
    <section>
      <h3>Sync history</h3>
      {events.length === 0 ? (
        <p className="note">No sync events recorded for this load.</p>
      ) : (
        <>
          <p className="note">
            {events.length} event{events.length === 1 ? "" : "s"}, in arrival order, each shown
            as its TMS stated it. A later file that restates a value appears as a new entry —
            nothing above it was rewritten.
          </p>
          {events.map((event, index) => (
            <SyncEntry
              key={event.id}
              event={event}
              // A rule change between two files is the thing worth seeing, so the
              // file gets a heading whenever it differs from the entry above.
              startsFile={index === 0 || events[index - 1].sync_file !== event.sync_file}
            />
          ))}
        </>
      )}
    </section>
  );
}

function SyncEntry({ event, startsFile }: { event: SyncEvent; startsFile: boolean }) {
  return (
    <>
      {startsFile && (
        <h4 className="sync-file">
          {event.sync_file} <span className="muted">· received {instant(event.synced_at)}</span>
        </h4>
      )}
      <details className="sync-event" open>
        <summary>
          <span className="entity">{event.entity_type}</span> {event.source_entity_id}{" "}
          <span className="muted">(event #{event.event_seq} in this file)</span>
        </summary>
        <pre>{JSON.stringify(event.raw_json, null, 2)}</pre>
      </details>
    </>
  );
}
