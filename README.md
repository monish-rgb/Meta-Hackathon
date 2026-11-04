# FlowGate - Agent Information-Flow Gateway

> *Sandboxes isolate execution. We isolate information.*

Sandboxed AI agent runs still leak information to each other through shared tools - documents, caches, databases, MCP servers. A run can copy or summarise sensitive data into a shared resource, and another run that was never granted access can read it back. Each individual trace looks innocent; only the cross-run view shows the leak.

FlowGate is an externally enforced gateway between agent runs and their protected shared tools. It labels every run and resource, tracks data lineage across runs, enforces policy **before** a tool call executes, and blocks, suspends, and alerts on unauthorised flows.

## How it works

1. **Labels** - every run gets a `project` + `clearance` (0 public / 1 internal / 2 secret), a bearer token, and an *effective label* (classification + compartments, starting at level 0 with no compartments); every resource gets a `project` + `sensitivity`.
2. **Policy** - YAML rules ([backend/policies.yaml](backend/policies.yaml)) decide allow/deny from labels alone: cross-project access denied, reads above clearance denied.
3. **Flow** — every allowed read joins the read resource's label into the run's effective label (max classification, union of compartments). A write is allowed only when the effective label can flow to the destination label (classification <= and compartments subset); otherwise it is blocked as `deny-write-down` (no suspension).
4. **Lineage** - at startup/reset, every non-public resource (sensitivity > 0) with content is fingerprinted in advance (`first_read_by = "seed"`, idempotently), and every allowed read stores a fingerprint of the content (SHA-256, canary tokens, word 5-shingles) tied to its source resource, project, and sensitivity.
5. **Detection** - every write that passes policy and flow is scanned against fingerprints from other projects / higher sensitivity: exact copy, canary token, or Jaccard overlap ≥ 0.30 → **blocked and the run is suspended instantly**; Jaccard 0.15–0.30 → blocked with alert; and if ≥ 50% of the smaller shingle set is shared (containment), the write is blocked as suspicious overlap even below Jaccard 0.15 — without suspension. Event reasons never include the canary value itself.
6. **Record** - every attempt (allowed and blocked, with the policy/detection reason) is appended to a SQLite event store and visualised as a live lineage graph. For local execution, execution, versioning, receipt, and event commit in one transaction, so a write never persists without its audit record. For externalised execution (separate tool/file backends) no transaction can span the service boundary, so the gateway instead records a durable execution intent before dispatch, deduplicates by request ID, reports ambiguous outcomes as `unknown` (never `blocked`), and reconciles on retry (Tier 4); restarts preserve stored content (only explicit demo reset reseeds).
7. **Artifacts & receipts** - runs can create new artifacts (`POST /artifacts`); the server assigns the label from the creator's effective label (clients never choose it) and every write appends an immutable version. Allowed tool executions return an execution receipt; denied calls produce none (`GET /tool-receipts`), so "the denied write never executed" is provable, not just inferred.

## Quickstart

Backend (Python 3.12+). Admin endpoints (`POST /runs`, suspend/reactivate,
`POST /demo/*`) and all inspection GETs require an admin key. Set
`FLOWGATE_ADMIN_KEY` (falls back to `GATEWAY_ADMIN_KEY`) — or, for a local
demo only, explicitly enable demo mode with `FLOWGATE_DEMO_MODE=1`, which
accepts the dev key `flowgate-dev-admin`. With neither, the backend is
fail-closed (401 `admin-required`). Send the key as `X-Admin-Key: <key>` or
`Authorization: Bearer <key>`. The dashboard never bundles a key: enter it
in the header field and it is kept only in the browser session.

```powershell
cd backend
$env:FLOWGATE_DEMO_MODE = "1"   # local demo only; or set FLOWGATE_ADMIN_KEY
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000
```

Dashboard (Node 18+), in a second terminal:

```powershell
cd frontend
npm install
npm run dev        # http://localhost:5173
```

Then open the dashboard and click **Run Attack Demo**.

## The demo (8-step twin scenario)

Two twin pairs with identical labels show that flow follows the *effective*
label built from reads, not just who the run is:

| # | Actor | Action | Result |
|---|---|---|---|
| 1 | A (public, clearance 0) | Reads `public-source` | ✅ Allowed |
| 2 | A (public) | Writes the public draft to `public-draft` | ✅ Allowed |
| 3 | B (public) | Reads `public-draft` | ✅ Allowed |
| 4 | B (public) | Writes the draft content to `public-outbox` | ✅ Allowed |
| 5 | A (alpha, clearance 2) | Reads the secret Project Nova roadmap (`alpha-roadmap`) | ✅ Allowed |
| 6 | A (alpha) | Creates a new artifact `alpha-plan-draft` — server assigns inherited label (secret/alpha), version 1 | ✅ Allowed |
| 7 | B (alpha) | Reads `alpha-plan-draft` (effective label becomes secret/alpha) | ✅ Allowed |
| 8 | B (alpha) | Writes the draft content to `public-outbox` | ⛔ Blocked (`deny-write-down`), **not suspended**, outbox unchanged, no execution receipt |

Also runnable headless: start the backend, then `python scripts/demo.py --admin-key <key>` (step 9 externally re-verifies that the denied write produced no tool execution receipt). API-only: `POST /demo/run`, `POST /demo/reset` (both admin-gated).

## Real-workflow integration (Tier 5)

- **Client library** — `flowgate_client/` is an importable httpx client (`create_run`, tool calls, artifacts, receipts/intents); `scripts/agent_demo.py` runs one real-LLM step through it (the model proposes, the gateway disposes; restricted content never enters model context).
- **File-docs connector** — `backend/file_docs_service.py` serves real document files behind the same gateway-held key + request-id dedupe protocol, so tenant documents live outside the gateway store.
- **Clean-context spawning** — a controller can spawn a `clean` run (fresh label, recorded lineage, for innocent public output) or a `continuing` run (server-side inherits the parent's current label — a fresh token never launders provenance). Suspension means exactly that a run's gateway access is revoked.
- **Flagship demo** — `python scripts/tenant_assistant_demo.py` boots both services and runs the multi-tenant story end-to-end: private brief → inherited-label draft → blocked publish → cross-tenant block → clean spawn publishes, continuing spawn blocked.

## Tests

```powershell
cd backend
python -m pytest tests/ -q     # detection ladder, policy + flow evaluator, gateway e2e (admin gating, twin demo)
cd ../frontend
npm run build
```

## Project layout

- [CONTRACT.md](CONTRACT.md) — frozen API/label/detection contract the components were built against
- [backend/app/](backend/app/) — gateway (`main.py`), SQLite store, auth, policy engine, detection, protected tools (`shared_doc`, `shared_kv`)
- [backend/seed.py](backend/seed.py) — demo resources (Alpha roadmap with canary, Beta notes, shared board)
- [frontend/src/](frontend/src/) — dashboard: live event feed, React Flow lineage graph, runs panel, attack simulator
- [scripts/demo.py](scripts/demo.py) — external demo client
- [.claude/agents/](.claude/agents/) — project subagents used to build this (backend, detection, tools/seed, frontend, demo/tests, integration, adversarial verifier). They activate as named agent types after a Claude Code restart; this build ran the same roles via parallel general-purpose agents.

## Scope (honest limits)

FlowGate protects tools routed through the gateway. Its principal guarantee is the label flow rule for reads tracked through the gateway; the fingerprint scanner is a backstop that catches verbatim, near-verbatim, canary-bearing, and fragmented-but-overlapping copies (including writes by runs that never read the source here, thanks to seeded fingerprints). A paraphrase with near-zero shingle overlap by a writer who never read through the gateway, and channels outside the gateway, remain out of scope for this MVP. Detection is deliberately deterministic and explainable: every block names the policy or fingerprint that caused it (canary values are never written into event reasons).
