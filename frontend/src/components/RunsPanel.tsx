import { useState } from 'react';
import { reactivateRun, suspendRun } from '../api';
import type { Run } from '../api';

const CLEARANCE_NAMES = ['public', 'internal', 'secret'];

interface Props {
  runs: Run[];
  onChanged: () => void;
}

export default function RunsPanel({ runs, onChanged }: Props) {
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const act = async (runId: string, fn: (id: string) => Promise<unknown>) => {
    setPending(runId);
    setError(null);
    try {
      await fn(runId);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  if (runs.length === 0) {
    return <p className="muted">No runs yet.</p>;
  }

  return (
    <div>
      {error && <div className="banner error">{error}</div>}
      <ul className="runs">
        {runs.map((r) => {
          const levelName =
            CLEARANCE_NAMES[r.effective_level ?? r.clearance] ?? '?';
          const compartments = r.effective_compartments ?? [];
          const contextLabel =
            compartments.length > 0
              ? `${levelName} {${compartments.join(', ')}}`
              : levelName;
          return (
            <li key={r.run_id} className="run">
              <div className="run-info">
                <code title={r.run_id}>{r.run_id.slice(0, 8)}…</code>
                <span>project: {r.project}</span>
                <span>tenant: {r.tenant ?? r.project}</span>
                <span>
                  clearance: {r.clearance} ({CLEARANCE_NAMES[r.clearance] ?? '?'})
                </span>
                <span>context: {contextLabel}</span>
                {r.spawn_mode && (
                  <span
                    className={`badge ${
                      r.spawn_mode === 'clean' ? 'spawn-clean' : 'spawn-continuing'
                    }`}
                  >
                    {r.spawn_mode === 'clean'
                      ? 'clean spawn'
                      : r.spawn_mode === 'continuing'
                        ? 'continuing spawn'
                        : `${r.spawn_mode} spawn`}
                  </span>
                )}
                {r.parent_run_id && (
                  <span>
                    parent:{' '}
                    <code title={r.parent_run_id}>
                      {r.parent_run_id.slice(0, 8)}…
                    </code>
                  </span>
                )}
                {r.suspended && <span className="badge suspended">suspended</span>}
              </div>
            {r.suspended ? (
              <button
                className="secondary"
                disabled={pending === r.run_id}
                onClick={() => void act(r.run_id, reactivateRun)}
              >
                Reactivate
              </button>
            ) : (
              <button
                disabled={pending === r.run_id}
                onClick={() => void act(r.run_id, suspendRun)}
              >
                Suspend
              </button>
            )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
