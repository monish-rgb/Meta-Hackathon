import { useState } from 'react';
import { runChallenge } from '../api';
import type { ChallengeResult, ChallengeWriter } from '../api';

interface Props {
  hasAdminKey: boolean;
}

const MAX_CONTENT = 2000;

export default function ChallengePanel({ hasAdminKey }: Props) {
  const [content, setContent] = useState('');
  const [writer, setWriter] = useState<ChallengeWriter>('fresh');
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<ChallengeResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const submitDisabled = !hasAdminKey || content.length === 0 || submitting;

  const handleSubmit = async () => {
    if (submitDisabled) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await runChallenge({ content, writer });
      setResult(res);
    } catch (err) {
      setResult(null);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="challenge-grid">
      <div className="challenge-form">
        <div>
          <label htmlFor="challenge-content">Challenge content</label>
          <textarea
            id="challenge-content"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            maxLength={MAX_CONTENT}
            rows={6}
            aria-describedby="challenge-count"
            placeholder="Text the selected writer will try to write to challenge-outbox through the gateway…"
          />
          <div id="challenge-count" className="muted" aria-live="polite">
            {content.length} / {MAX_CONTENT} characters
          </div>
        </div>

        <fieldset className="writer-options">
          <legend>Writer</legend>
          <label>
            <input
              type="radio"
              name="challenge-writer"
              value="fresh"
              checked={writer === 'fresh'}
              onChange={() => setWriter('fresh')}
            />{' '}
            fresh — read nothing, scanner decides
          </label>
          <label>
            <input
              type="radio"
              name="challenge-writer"
              value="tainted"
              checked={writer === 'tainted'}
              onChange={() => setWriter('tainted')}
            />{' '}
            tainted — read the alpha roadmap first, flow check decides
          </label>
        </fieldset>

        {!hasAdminKey && (
          <p className="muted">Enter the backend admin key above to run a challenge.</p>
        )}

        <button onClick={() => void handleSubmit()} disabled={submitDisabled}>
          {submitting ? 'Running…' : 'Run Challenge (gated)'}
        </button>

        {error && (
          <div className="banner error" role="alert">
            {error}
          </div>
        )}

        {result && (
          <article
            className={`event ${result.decision === 'blocked' ? 'blocked' : 'allowed'}`}
            aria-label="Gated challenge result"
          >
            <div className="event-head">
              <span className="badge">{result.decision}</span>
              <span className="badge">layer: {result.layer}</span>
              <span className="muted">gated result</span>
            </div>
            <dl className="evidence-fields">
              <div>
                <dt>policy</dt>
                <dd>
                  <code>{result.policy_id}</code>
                </dd>
              </div>
              <div>
                <dt>similarity</dt>
                <dd>
                  {result.similarity !== null && result.similarity !== undefined
                    ? result.similarity
                    : 'n/a'}
                </dd>
              </div>
              <div>
                <dt>suspended</dt>
                <dd>{result.suspended ? 'yes' : 'no'}</dd>
              </div>
              <div>
                <dt>writer</dt>
                <dd>{result.writer}</dd>
              </div>
            </dl>
            {result.reason && <p className="event-reason">reason: {result.reason}</p>}
          </article>
        )}
      </div>

      <aside className="counterfactual" aria-label="Naive counterfactual, not executed">
        <h3>Naive (counterfactual — not executed)</h3>
        <p>
          Naive (counterfactual — not executed): without the gateway this write would
          land in the shared store unchecked.
        </p>
        <p className="muted">
          This panel is presentational only. No ungated write is performed here; the
          only write attempted is the gated challenge on the left.
        </p>
      </aside>
    </div>
  );
}
