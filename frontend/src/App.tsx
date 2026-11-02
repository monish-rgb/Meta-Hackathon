import { useCallback, useEffect, useState } from 'react';
import {
  apiBase,
  getAdminKey,
  getGraph,
  listAlerts,
  listEvents,
  listResources,
  listRuns,
  resetDemo,
  runDemo,
  setAdminKey,
} from './api';
import type { AlertItem, DemoResult, EventItem, Graph, Resource, Run } from './api';
import EventFeed from './components/EventFeed';
import EvidenceWall from './components/EvidenceWall';
import ChallengePanel from './components/ChallengePanel';
import LineageGraph from './components/LineageGraph';
import RunsPanel from './components/RunsPanel';
import SimulatorPanel from './components/SimulatorPanel';

export default function App() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [events, setEvents] = useState<EventItem[]>([]);
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [graph, setGraph] = useState<Graph>({ nodes: [], edges: [] });
  const [resources, setResources] = useState<Resource[]>([]);
  const [demo, setDemo] = useState<DemoResult | null>(null);
  const [demoRunning, setDemoRunning] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [adminInput, setAdminInput] = useState('');
  const [hasAdminKey, setHasAdminKey] = useState(() => Boolean(getAdminKey()));

  const refresh = useCallback(async () => {
    try {
      const [r, e, g, res, al] = await Promise.all([
        listRuns(),
        listEvents({ limit: 200 }),
        getGraph(),
        listResources(),
        listAlerts(50),
      ]);
      setRuns(r);
      setEvents(e);
      setGraph(g);
      setResources(res);
      setAlerts(al.alerts);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = window.setInterval(() => {
      void refresh();
    }, 1500);
    return () => window.clearInterval(t);
  }, [refresh]);

  const handleRunDemo = async () => {
    setDemoRunning(true);
    setError(null);
    try {
      const result = await runDemo();
      setDemo(result);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDemoRunning(false);
    }
  };

  const handleReset = async () => {
    setResetting(true);
    setError(null);
    try {
      await resetDemo();
      setDemo(null);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setResetting(false);
    }
  };

  const handleSaveAdminKey = async () => {
    setAdminKey(adminInput.trim());
    setHasAdminKey(Boolean(adminInput.trim()));
    setAdminInput('');
    await refresh();
  };

  const handleClearAdminKey = async () => {
    setAdminKey('');
    setHasAdminKey(false);
    await refresh();
  };

  return (
    <div className="page">
      <header className="header">
        <div>
          <h1>FlowGate</h1>
          <p className="subtitle">FlowGate — Agent Information-Flow Gateway</p>
          <p className="tagline">Sandboxes isolate execution. We isolate information.</p>
        </div>
        <div className="controls">
          <input
            type="password"
            value={adminInput}
            onChange={(e) => setAdminInput(e.target.value)}
            placeholder={hasAdminKey ? 'Admin key saved (session)' : 'Backend admin key'}
            aria-label="Backend admin key"
          />
          <button className="secondary" onClick={() => void handleSaveAdminKey()}>
            Save key
          </button>
          <button className="secondary" onClick={() => void handleClearAdminKey()}>
            Clear
          </button>
          <button onClick={() => void handleRunDemo()} disabled={demoRunning}>
            {demoRunning ? 'Running…' : 'Run Flow Demo'}
          </button>
          <button
            className="secondary"
            onClick={() => void handleReset()}
            disabled={resetting}
          >
            {resetting ? 'Resetting…' : 'Reset'}
          </button>
        </div>
      </header>

      {!hasAdminKey && (
        <div className="banner">
          Enter the backend admin key to load dashboard data — stored only in
          this browser session, never bundled with the app.
        </div>
      )}

      {error && (
        <div className="banner error">
          Backend unreachable at {apiBase}: {error}
        </div>
      )}

      <main className="grid">
        <section className="panel">
          <h2>Runs</h2>
          <RunsPanel runs={runs} onChanged={() => void refresh()} />
        </section>
        <section className="panel">
          <h2>Flow Simulator</h2>
          <SimulatorPanel result={demo} running={demoRunning} />
        </section>
        <section className="panel">
          <h2>Judge Challenge</h2>
          <ChallengePanel hasAdminKey={hasAdminKey} />
        </section>
        <section className="panel wide">
          <h2>Evidence Wall</h2>
          <EvidenceWall events={events} graph={graph} runs={runs} />
        </section>
        <section className="panel wide">
          <h2>Lineage Graph</h2>
          <LineageGraph nodes={graph.nodes} edges={graph.edges} />
        </section>
        <section className="panel wide">
          <h2>Resources</h2>
          {resources.length === 0 ? (
            <p className="muted">No resources yet.</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>resource_id</th>
                  <th>kind</th>
                  <th>project</th>
                  <th>tenant</th>
                  <th>label</th>
                  <th>version</th>
                </tr>
              </thead>
              <tbody>
                {resources.map((r: Resource) => (
                  <tr key={r.resource_id}>
                    <td>
                      <code>{r.resource_id}</code>
                    </td>
                    <td>{r.kind}</td>
                    <td>{r.project}</td>
                    <td>{r.tenant ?? r.project}</td>
                    <td>
                      {r.sensitivity}
                      {(r.compartments ?? []).length > 0
                        ? ` / ${(r.compartments ?? []).join(', ')}`
                        : ''}
                    </td>
                    <td>{r.version ?? 1}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
        <section className="panel wide">
          <h2>Alerts — blocked attempts ({alerts.length})</h2>
          {alerts.length === 0 ? (
            <p className="muted">No blocked attempts recorded.</p>
          ) : (
            <EventFeed events={alerts} />
          )}
        </section>
        <section className="panel wide">
          <h2>Events</h2>
          <EventFeed events={events} />
        </section>
      </main>
    </div>
  );
}
