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
 * The *list* of events is always open — that is the fact a rep might act on.
 * Only the raw payload behind each one folds away, because nobody reads a TMS's
 * JSON fifty times a day, and unfolding it is the deliberate act of auditing.
 *
 * No fetch of its own: the history rides along on `/api/loads/{id}`, so the
 * detail screen's loading and error states already cover it.
 */

import { instant } from "./format";
import type { SyncEvent } from "./types";

interface Props {
  events: SyncEvent[];
  createdAt: string | null;
  lastModifiedAt: string | null;
}

export function SyncHistoryPanel({ events, createdAt, lastModifiedAt }: Props) {
  return (
    <section className="panel">
      <div className="panel-hd">
        <h3>Sync history</h3>
        <span className="sub">
          {events.length} event{events.length === 1 ? "" : "s"}, in arrival order, each as its
          TMS stated it
        </span>
        <span className="spacer" />
        <span className="sub mono">
          created {instant(createdAt)} · modified {instant(lastModifiedAt)}
        </span>
      </div>

      {events.length === 0 ? (
        <p className="state">No sync events recorded for this load.</p>
      ) : (
        <>
          <div className="panel-bd">
            {events.map((event, index) => (
              <SyncEntry
                key={event.id}
                event={event}
                // A file boundary is the thing worth seeing, so the file gets a
                // heading whenever it differs from the entry above.
                startsFile={index === 0 || events[index - 1].sync_file !== event.sync_file}
              />
            ))}
          </div>
          <div className="note">
            A later file that restates a value appears as a <b>new entry</b> — nothing above it
            was rewritten. Open an entry to read the payload exactly as that TMS sent it.
          </div>
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
          {event.sync_file}
          <span className="when">received {instant(event.synced_at)}</span>
        </h4>
      )}
      <details className="sync-event">
        <summary>
          <span className="entity">{event.entity_type}</span>
          <span className="mono">{event.source_entity_id}</span>{" "}
          <span className="muted">(event #{event.event_seq} in this file)</span>
        </summary>
        <pre>{JSON.stringify(event.raw_json, null, 2)}</pre>
      </details>
    </>
  );
}
