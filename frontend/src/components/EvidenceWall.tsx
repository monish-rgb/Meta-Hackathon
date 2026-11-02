import { useEffect, useState } from 'react';
import { listToolReceipts } from '../api';
import type { EventItem, Graph, Run } from '../api';

interface Props {
  events: EventItem[];
  graph: Graph;
  runs: Run[];
}

const OUTBOX = 'public-outbox';

export default function EvidenceWall({ events, graph, runs }: Props) {
  const [blockedReceiptCount, setBlockedReceiptCount] = useState<number | null>(null);
  const [allowedOutboxWriteCount, setAllowedOutboxWriteCount] = useState<number | null>(
    null,
  );
  const [receiptError, setReceiptError] = useState<string | null>(null);

  // Latest = highest event_id (events are newest-first, but do not rely on order).
  const blockedEvent =
    events
      .filter(
        (e) =>
          e.decision === 'blocked' &&
          e.policy_id === 'deny-write-down' &&
          e.resource_id === OUTBOX,
      )
      .sort((a, b) => b.event_id - a.event_id)[0] ?? null;

  const blockedEventId = blockedEvent?.event_id ?? null;
  const blockedRunId = blockedEvent?.run_id ?? null;

  useEffect(() => {
    if (blockedEventId === null) {
      setBlockedReceiptCount(null);
      setAllowedOutboxWriteCount(null);
      setReceiptError(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const [blocked, overall] = await Promise.all([
          blockedRunId
            ? listToolReceipts({ run_id: blockedRunId, resource_id: OUTBOX })
            : Promise.resolve({ count: 0, receipts: [] }),
          listToolReceipts({ resource_id: OUTBOX }),
        ]);
        if (cancelled) return;
        setBlockedReceiptCount(blocked.count);
        // Receipts cover reads and writes; the comparison is allowed writes.
        setAllowedOutboxWriteCount(
          overall.receipts.filter((r) => !r.operation || r.operation === 'write').length,
        );
        setReceiptError(null);
      } catch (err) {
        if (cancelled) return;
        setBlockedReceiptCount(null);
        setAllowedOutboxWriteCount(null);
        setReceiptError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
    // Refetch when the events prop changes in a way that changes the blocked attempt.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blockedEventId, blockedRunId, events]);

  if (!blockedEvent) {
    return (
      <p className="muted">
        No blocked <code>deny-write-down</code> write to <code>public-outbox</code> yet —
        run the Flow Demo to produce one.
      </p>
    );
  }

  const run = blockedEvent.run_id
    ? runs.find((r) => r.run_id === blockedEvent.run_id)
    : undefined;
  const edge = graph.edges.find((e) => e.id === `ev:${blockedEvent.event_id}`);

  return (
    <div>
      <div className="evidence-grid">
        <article className="evidence-card" aria-label="Blocked event evidence">
          <h3>1. Blocked event</h3>
          <span className="badge">blocked</span>
          <dl className="evidence-fields">
            <div>
              <dt>policy</dt>
              <dd>
                <code>{blockedEvent.policy_id ?? '—'}</code>
              </dd>
            </div>
            <div>
              <dt>event</dt>
              <dd>#{blockedEvent.event_id}</dd>
            </div>
            <div>
              <dt>time</dt>
              <dd>{blockedEvent.ts}</dd>
            </div>
            <div>
              <dt>run</dt>
              <dd>
                {blockedEvent.run_id ? (
                  <code title={blockedEvent.run_id}>
                    {blockedEvent.run_id.slice(0, 8)}…
                  </code>
                ) : (
                  '—'
                )}
                {run ? ` (project: ${run.project})` : ''}
              </dd>
            </div>
            <div>
              <dt>similarity</dt>
              <dd>
                {blockedEvent.similarity !== null && blockedEvent.similarity !== undefined
                  ? blockedEvent.similarity
                  : 'n/a'}
              </dd>
            </div>
          </dl>
          {blockedEvent.reason && (
            <p className="event-reason">reason: {blockedEvent.reason}</p>
          )}
        </article>

        <article className="evidence-card" aria-label="Lineage edge evidence">
          <h3>2. Lineage edge</h3>
          {edge ? (
            <>
              <span className="badge">blocked attempt</span>
              <dl className="evidence-fields">
                <div>
                  <dt>edge id</dt>
                  <dd>
                    <code>{edge.id}</code>
                  </dd>
                </div>
                <div>
                  <dt>attempt</dt>
                  <dd>
                    <code>{edge.source}</code> → <code>{edge.target}</code>
                  </dd>
                </div>
                <div>
                  <dt>expected direction</dt>
                  <dd>
                    run → <code>res:public-outbox</code>
                  </dd>
                </div>
                <div>
                  <dt>operation</dt>
                  <dd>{edge.operation}</dd>
                </div>
                <div>
                  <dt>decision</dt>
                  <dd>{edge.decision}</dd>
                </div>
                <div>
                  <dt>policy</dt>
                  <dd>
                    <code>{edge.policy_id}</code>
                  </dd>
                </div>
              </dl>
              <p className="event-reason">
                Blocked attempt edge: the flow stopped at the outbox.
              </p>
            </>
          ) : (
            <p className="muted">
              No lineage edge <code>ev:{blockedEvent.event_id}</code> for this blocked
              attempt is present in the graph.
            </p>
          )}
        </article>

        <article className="evidence-card" aria-label="Execution evidence">
          <h3>3. Execution evidence</h3>
          {receiptError ? (
            <p className="muted" role="alert">
              Execution receipts could not be loaded: {receiptError}
            </p>
          ) : blockedReceiptCount === null ? (
            <p className="muted">Loading execution receipts…</p>
          ) : (
            <>
              <span className="badge">{blockedReceiptCount} executions</span>
              <dl className="evidence-fields">
                <div>
                  <dt>blocked attempt</dt>
                  <dd>
                    {blockedReceiptCount === 0
                      ? '0 executions for the blocked attempt — no execution receipt for this run and public-outbox'
                      : `${blockedReceiptCount} execution receipt(s) for this run and public-outbox`}
                  </dd>
                </div>
                <div>
                  <dt>allowed writes</dt>
                  <dd>
                    {allowedOutboxWriteCount === null
                      ? 'loading…'
                      : `${allowedOutboxWriteCount} receipted allowed write(s) to public-outbox overall`}
                  </dd>
                </div>
              </dl>
            </>
          )}
          <p className="event-reason">
            Receipt absence proves non-execution only where the gateway is the sole
            path to the tool backend (gateway-held tool key, no direct agent access).
          </p>
        </article>
      </div>
    </div>
  );
}
