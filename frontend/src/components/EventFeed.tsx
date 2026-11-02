import type { EventItem } from '../api';

export default function EventFeed({ events }: { events: EventItem[] }) {
  if (events.length === 0) {
    return <p className="muted">No events yet.</p>;
  }
  return (
    <div className="feed">
      {events.map((e) => (
        <div
          key={e.event_id}
          className={`event ${e.decision === 'blocked' ? 'blocked' : 'allowed'}`}
        >
          <div className="event-head">
            <span className="badge">{e.decision}</span>
            <span className="muted">#{e.event_id}</span>
            <span className="muted">{e.ts}</span>
          </div>
          <div className="event-fields">
            {e.run_id && (
              <span title={e.run_id}>
                run: <code>{e.run_id.slice(0, 8)}…</code>
              </span>
            )}
            {e.tool && <span>tool: {e.tool}</span>}
            {e.operation && <span>op: {e.operation}</span>}
            {e.resource_id && (
              <span>
                resource: <code>{e.resource_id}</code>
              </span>
            )}
            {e.policy_id && (
              <span>
                policy: <code>{e.policy_id}</code>
              </span>
            )}
            {e.similarity !== null && e.similarity !== undefined && (
              <span>similarity: {e.similarity}</span>
            )}
          </div>
          {e.reason && <div className="event-reason">{e.reason}</div>}
        </div>
      ))}
    </div>
  );
}
