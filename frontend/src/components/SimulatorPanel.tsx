import type { DemoResult } from '../api';

interface Props {
  result: DemoResult | null;
  running: boolean;
}

export default function SimulatorPanel({ result, running }: Props) {
  if (running) {
    return <p className="muted">Running flow demo…</p>;
  }
  if (!result) {
    return <p className="muted">Press “Run Flow Demo” to execute the flow scenario.</p>;
  }
  const passed = result.steps.filter((s) => s.ok).length;
  return (
    <div>
      <p>
        Passed {passed}/{result.steps.length} steps.
      </p>
      <ol className="steps">
        {result.steps.map((s) => (
          <li key={s.step} className={s.ok ? 'pass' : 'fail'}>
            <div>
              {s.ok ? '✅' : '⛔'} Step {s.step} — {s.actor}: {s.action}
            </div>
            <div className="muted">
              expected: {s.expected} · actual: {s.actual}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
