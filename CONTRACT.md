# CONTRACT.md — Frozen Build Contract (Phase 0)

This file is the single source of truth. Builder agents implement ONLY their owned files against this contract. Do not change semantics; report mismatches instead.

## Product

Gateway between agent runs and protected shared tools. Labels on runs/resources, lineage fingerprints on reads, detection scan on writes, policy enforced before execution, block/suspend/alert, append-only event log, dashboard with lineage graph.

## Ports / URLs

- Backend: `http://localhost:8000` (FastAPI, CORS allows `http://localhost:5173`)
- Frontend: `http://localhost:5173` (Vite), API base = `import.meta.env.VITE_API_URL ?? "http://localhost:8000"`

## Label model

- Run: `project` (string), `clearance` (0=public, 1=internal, 2=secret), `suspended` (bool)
- Resource: `resource_id` (string), `kind` (`doc`|`kv`), `project` (string or `"shared"`), `sensitivity` (0/1/2), `content` (string)
- Tool → kind mapping: `shared_doc` → `doc`, `shared_kv` → `kv`

## Policy semantics (`backend/app/policy.py` + `backend/policies.yaml`)

`policies.yaml` shape:

```yaml
default_action: deny
rules:
  - id: deny-cross-project
    action: deny
    reason: "Cross-project access is not allowed"
    when: { project_relation: different }
  - id: deny-insufficient-clearance
    action: deny
    reason: "Run clearance is below resource sensitivity"
    when: { clearance_vs_sensitivity: lt }
  - id: allow-label-match
    action: allow
    reason: "Labels allow this access"
    when: {}
```

Evaluator rules:
- Compute `project_relation`: `"shared"` if resource.project == `"shared"`, else `"same"` if run.project == resource.project, else `"different"`.
- Compute `clearance_vs_sensitivity`: `"lt"` if run.clearance < resource.sensitivity, `"gte"` otherwise.
- Condition keys supported in `when`: `operation` (string or list), `project_relation` (string), `clearance_vs_sensitivity` (string). All present keys must match; empty `when` matches everything.
- Evaluate rules in file order, **first match wins**. If no rule matches, use `default_action` with policy_id `"default-deny"`.
- `evaluate_policy(run: dict, resource: dict, operation: str, policies: list[dict]) -> {"action": "allow"|"deny"|"deny_and_suspend", "policy_id": str, "reason": str}` (`policies` = parsed `rules` list.)
- `load_policies(path: str) -> {"rules": [...], "default_action": str}`

Suspended runs are rejected BEFORE policy evaluation, with policy_id `"run-suspended"`.

## Detection semantics (`backend/app/detection.py`) — pure functions, no I/O

Constants: `SHINGLE_K = 5`, `DENY_THRESHOLD = 0.15`, `SUSPEND_THRESHOLD = 0.30`

- `normalize_content(content: str) -> str`: lowercase, collapse all whitespace runs to single space, strip.
- `content_hash(content: str) -> str`: SHA-256 hex of normalized content.
- `extract_canaries(content: str) -> list[str]`: regex `CANARY-[A-Z0-9]+(?:-[A-Z0-9]+)*`, sorted unique.
- `shingles(content: str, k: int = 5) -> set[str]`: tokens = `re.findall(r"[a-z0-9]+", content.lower())`; if no tokens → empty set; if `len(tokens) < k` → `{" ".join(tokens)}`; else all `" ".join(tokens[i:i+k])`.
- `jaccard(a: set, b: set) -> float`: 0.0 if either is empty; else `len(a∩b)/len(a∪b)`.
- `make_fingerprint(content: str) -> {"hash": str, "canaries": list[str], "shingles": list[str]}` (shingles sorted list for JSON storage).
- `scan_write(content: str, fingerprints: list[dict], run_project: str, target_sensitivity: int) -> dict`
  - Each fingerprint dict: `{"id", "source_resource_id", "source_project", "source_sensitivity", "hash", "canaries", "shingles"}` (shingles may be list or set).
  - Only consider fingerprints where `source_project != run_project` **OR** `source_sensitivity > target_sensitivity`. Ignore others.
  - For considered fingerprints, in severity order:
    1. `content_hash(content) == fp.hash` → `{"action": "deny_and_suspend", "policy_id": "detect-exact-copy", ...}`
    2. canary intersection non-empty → `deny_and_suspend`, policy_id `"detect-canary"`
    3. max jaccard ≥ 0.30 → `deny_and_suspend`, policy_id `"detect-taint-overlap"`
    4. max jaccard ≥ 0.15 → `deny`, policy_id `"detect-suspicious-overlap"`
    5. else → `allow`, policy_id `"detect-clean"`
  - Return always: `{"action", "policy_id", "reason", "similarity": float (max jaccard, rounded 4dp), "matched_fingerprint_id": int|None, "matched_canary": str|None}`. Reason is a human sentence including the score/canary when relevant.

## Store (`backend/app/store.py`, SQLite) & models (`backend/app/models.py`)

Tables: `runs(run_id TEXT PK, project TEXT, clearance INT, suspended INT, token TEXT, created_at TEXT)`, `resources(resource_id TEXT PK, kind TEXT, project TEXT, sensitivity INT, content TEXT, updated_at TEXT)`, `events(event_id INTEGER PK AUTOINCREMENT, ts TEXT, run_id TEXT, tool TEXT, operation TEXT, resource_id TEXT, decision TEXT, action TEXT, policy_id TEXT, reason TEXT, similarity REAL, content_hash TEXT)`, `fingerprints(fingerprint_id INTEGER PK AUTOINCREMENT, source_resource_id TEXT, source_project TEXT, source_sensitivity INT, first_read_by TEXT, hash TEXT, canaries TEXT, shingles TEXT, created_at TEXT)` (canaries/shingles = JSON strings).

Store functions (all take `conn` first): `init_db(db_path) -> conn` (creates tables, `row_factory` = dict-like rows), `create_run(conn, project, clearance, token) -> dict`, `get_run_by_token(conn, token) -> dict|None`, `get_run(conn, run_id) -> dict|None`, `list_runs(conn) -> list[dict]`, `set_suspended(conn, run_id, suspended: bool) -> dict|None`, `upsert_resource(conn, resource_id, kind, project, sensitivity, content) -> dict`, `get_resource(conn, resource_id) -> dict|None`, `list_resources(conn) -> list[dict]`, `set_resource_content(conn, resource_id, content) -> None`, `insert_event(conn, run_id, tool, operation, resource_id, decision, action, policy_id, reason, similarity=None, content_hash=None) -> int`, `list_events(conn, run_id=None, decision=None, limit=200) -> list[dict]` (newest first), `insert_fingerprint(conn, source_resource_id, source_project, source_sensitivity, first_read_by, fingerprint: dict) -> int`, `list_fingerprints(conn) -> list[dict]` (shingles/canaries parsed back to lists), `reset_all(conn) -> None` (delete runs/events/fingerprints; resources are reseeded by caller).

Run dict shape everywhere: `{"run_id", "project", "clearance", "suspended": bool, "created_at"}` (+ `"token"` only at creation). Resource dict: `{"resource_id", "kind", "project", "sensitivity", "content"}` (API list view omits `content`).

`models.py`: Pydantic — `RunCreate{project: str, clearance: int (0..2)}`, `ToolCallRequest{operation: "read"|"write", resource_id: str, content: str|None = None}`.

## Auth (`backend/app/auth.py`)

- `create_token() -> str` (`secrets.token_urlsafe(32)`)
- `extract_bearer(authorization: str|None) -> str|None`
- `authenticate(conn, authorization) -> dict|None` (run dict via token)

## Tools (`backend/app/tools.py`) & seed (`backend/seed.py`)

- `TOOL_KIND = {"shared_doc": "doc", "shared_kv": "kv"}`
- `execute_tool(conn, tool_name, operation, resource: dict, content: str|None) -> str|None`: read → returns resource content; write → persists via store, returns None. Assumes already authorised; raises `ValueError` on unknown tool/operation.
- `seed_data(conn) -> None` inserts (upsert):
  - `alpha-roadmap` (doc, alpha, 2): Project Nova roadmap text, ≥60 words, MUST contain canary `CANARY-ALPHA-NOVA-7F3K9` and phrases about "Project Nova", launch date, budget. Also export this text as `ROADMAP_CONTENT` and a lightly-edited summary (≥50% shingle overlap with roadmap) as `LEAK_SUMMARY` from seed.py.
  - `alpha-notes` (doc, alpha, 1): short internal Alpha note.
  - `beta-notes` (doc, beta, 1): short internal Beta note, vocabulary clearly different from roadmap.
  - `shared-board` (kv, shared, 0): `""`
  - `shared-scratch` (kv, shared, 0): `""`

## Gateway (`backend/app/main.py`)

`create_app(db_path: str|None = None, policies_path: str|None = None) -> FastAPI`; module-level `app = create_app()`. Defaults: DB = `backend/gateway.db` (env `GATEWAY_DB` override), policies = `backend/policies.yaml`. On startup: init DB + `seed_data`. CORS: allow `http://localhost:5173`.

Endpoints:
- `GET /health` → `{"status": "ok"}`
- `POST /runs` (RunCreate) → 201 run dict **with token**
- `GET /runs` → list of run dicts (no token)
- `POST /runs/{run_id}/suspend` / `POST /runs/{run_id}/reactivate` → run dict (404 if unknown)
- `GET /resources` → list without content
- `POST /tools/{tool_name}` — header `Authorization: Bearer <token>`, body ToolCallRequest:
  - Unknown token → 401 `{"decision": "blocked", "reason": "Invalid or missing run token", "policy_id": "auth-failed", "event_id": null}` (no event; no run known)
  - Suspended run → record event, 403, policy_id `run-suspended`
  - Unknown tool → record event, 403, policy_id `unknown-tool`; unknown resource → 403 `resource-not-found`; tool/kind mismatch → 403 `tool-resource-mismatch`
  - Policy deny → record event (decision `blocked`), 403 `{decision, reason, policy_id, event_id}`
  - Write: run `scan_write` against ALL stored fingerprints with run.project + resource.sensitivity. If action != allow → record event with similarity, apply suspension if `deny_and_suspend`, 403 (include `similarity`).
  - Allowed write → execute, record event `allowed`, 200 `{"decision": "allowed", "reason", "policy_id" (from policy or detect-clean), "event_id", "result": null}`
  - Allowed read → execute, **store fingerprint** of returned content (source = this resource, first_read_by = run), record event, 200 with `"result": <content>`
  - Every recorded event: decision `"allowed"`/`"blocked"`, action = policy/detection action.
- `GET /events?run_id=&decision=&limit=` → list, newest first: `{"event_id", "ts", "run_id", "tool", "operation", "resource_id", "decision", "action", "policy_id", "reason", "similarity"}`
- `GET /graph` → `{"nodes": [...], "edges": [...]}`. Nodes: runs `{"id": "run:<run_id>", "label": "<project> run", "type": "run", "project", "clearance", "suspended"}`; resources `{"id": "res:<resource_id>", "label": resource_id, "type": "resource", "project", "sensitivity"}`. Edges from events: read → source `res:...`, target `run:...`; write → source `run:...`, target `res:...`; `{"id": "ev:<event_id>", "source", "target", "operation", "decision", "policy_id"}`.
- `POST /demo/reset` → reset_all + seed → `{"status": "reset"}`
- `POST /demo/run` → reset, then perform the demo table below internally (create Alpha/Beta runs, call the same logic as tool endpoints — direct function calls are fine, HTTP self-calls are not), return `{"steps": [{"step": int, "actor": str, "action": str, "expected": str, "actual": str, "ok": bool}], "alpha_run_id", "beta_run_id"}`.

### Demo decision table (also the e2e test oracle)

| # | Actor | Action | Expected |
|---|---|---|---|
| 1 | Alpha (alpha, 2) | read `alpha-roadmap` via shared_doc | allowed |
| 2 | Alpha | write `LEAK_SUMMARY` to `shared-board` via shared_kv | blocked, deny_and_suspend (taint or canary), Alpha suspended |
| 3 | Alpha | read `alpha-notes` (any call) | blocked, policy `run-suspended` |
| 4 | Beta (beta, 1) | read `alpha-roadmap` | blocked, policy `deny-cross-project` |
| 5 | Beta | write clean note (own words, unrelated to roadmap) to `shared-board`, then read `shared-board` | allowed, allowed |

## Frontend (`frontend/`, Vite + React + TS)

Deps: `react`, `react-dom`, `reactflow` (v11 package name `reactflow`). Scripts: `dev` (port 5173), `build`, `preview`.

- `src/api.ts`: typed fetch wrappers for all endpoints above.
- `src/App.tsx`: header (product name "FlowGate — Agent Information-Flow Gateway", tagline), **Run Attack Demo** + **Reset** buttons, auto-refresh (poll events+graph+runs every 1.5s).
- Components: `EventFeed` (table/list, blocked rows red, allowed green, show policy_id + reason + similarity), `LineageGraph` (reactflow; run nodes vs resource nodes visually distinct, blocked edges in red), `RunsPanel` (runs with project/clearance/suspended badge + suspend/reactivate buttons), `SimulatorPanel` (demo step results from POST /demo/run with ✅/⛔ per step).
- Dark, clean, single-page. Must build with `npm run build` with backend NOT running (no build-time API calls).

## Demo CLI (`scripts/demo.py`) & e2e tests (`backend/tests/test_gateway_e2e.py`)

- `demo.py`: stdlib + `httpx` (or urllib) against `http://localhost:8000`; registers Alpha/Beta, reads roadmap as Alpha, builds leak text by lightly editing the read content (replace a few words — do NOT import backend seed), runs the 5-step table printing ✅/⛔, exits non-zero on mismatch. `--base-url` arg.
- `test_gateway_e2e.py`: FastAPI `TestClient` with `create_app(db_path=tmp, policies_path=backend/policies.yaml)`; asserts the full 5-step table incl. suspension + clean-write no-false-positive. May import `LEAK_SUMMARY` from `backend/seed.py`.
- `test_detection.py`: hash/normalize/canary/shingle/jaccard units + scan_write severity ladder (exact, canary, ≥0.30, 0.15–0.30, clean, same-project-ignored).
- `test_policy.py`: project_relation/clearance cases incl. shared resource, first-match order, default deny.

## Requirements (`backend/requirements.txt`, written in Phase 0)

fastapi, uvicorn, pydantic, PyYAML, pytest, httpx.

## Out of scope

Real LLM agents, MCP protocol server, Postgres/Redis, embeddings/LLM judge, auth beyond run tokens.


---

# Tier 1 Amendment (supersedes conflicting text above)

This amendment is clearly marked and additive: the original contract text above
is kept for history. Where the amendment and the original text conflict, the
amendment wins.

## Admin gating and key resolution

- `create_app(db_path=None, policies_path=None, admin_key=None)`; the resolved
  key is stored as `app.state.admin_key`.
- Resolution order: `admin_key` argument > `FLOWGATE_ADMIN_KEY` env >
  `GATEWAY_ADMIN_KEY` env > `"flowgate-dev-admin"`.
- A request is an admin request when it carries `X-Admin-Key: <key>` **or**
  `Authorization: Bearer <key>` matching the resolved key
  (`secrets.compare_digest`).
- Admin-gated endpoints: `POST /runs`, `POST /runs/{id}/suspend`,
  `POST /runs/{id}/reactivate`, `POST /demo/reset`, `POST /demo/run`.
  Without a valid admin key they return HTTP 401 with
  `{"decision": "blocked", "reason": ..., "policy_id": "admin-required", "event_id": null}`.
- Known Tier 2 gap (intentional): all GET endpoints (`GET /health`,
  `GET /runs`, `GET /resources`, `GET /events`, `GET /graph`) remain open
  without any key. `POST /tools/{tool_name}` is authenticated by run token,
  not by admin key.

## Label model, join, and flow rule

- `RunCreate` adds `effective_level` (int 0..2, default 0) and
  `effective_compartments` (list of strings, default `[]`). Run dicts now also
  carry `effective_level` (int) and `effective_compartments` (sorted list);
  the `runs` table stores them (`effective_level INTEGER NOT NULL DEFAULT 0`,
  `effective_compartments TEXT NOT NULL DEFAULT '[]'`, migrated in `init_db`
  via `PRAGMA table_info(runs)` + `ALTER TABLE` when missing).
- A label is `{"classification": int, "compartments": sorted list}`.
  - `resource_label(resource)`: classification = resource sensitivity;
    compartments = `[]` when sensitivity is 0 or project is `"shared"`,
    otherwise `[project]`.
  - `run_effective_label(run)`: the run's effective label from its
    `effective_*` fields (missing fields default to level 0 / no compartments).
  - `join_labels(a, b)`: max classification, union of compartments.
  - `can_flow(source, dest)`: true iff source classification <= destination
    classification **and** source compartments are a subset of destination
    compartments.
  - `evaluate_flow(run, resource)`: `{"action": "allow", "policy_id": "flow-allowed", ...}`
    when the run effective label can flow to the resource label, otherwise
    `{"action": "deny", "policy_id": "deny-write-down", "reason": ...}` where
    the reason names both the effective and destination labels.
- Tool-call check ordering in `_handle_tool_call` is now:
  suspended run -> unknown tool / resource / kind mismatch -> YAML policy
  (`evaluate_policy`) -> flow check (`evaluate_flow`, writes only) ->
  detection scan (`scan_write`, writes only) -> execute. A flow deny records
  a blocked event with action `"deny"` and returns 403 with
  `{decision, reason, policy_id, event_id, effective_label, destination_label}`;
  it never suspends the run.
- On an allowed read the gateway executes, stores the content fingerprint,
  then joins the read resource's label into the run's effective label
  (`update_run_effective_label`) before responding; the 200 body includes the
  updated `effective_level` and `effective_compartments`.

## New fixtures (seed)

In addition to the original five resources, `seed.py` provides:
`alpha-draft` (doc, alpha, 2), `public-source` (doc, shared, 0),
`public-draft` (doc, shared, 0), `public-outbox` (kv, shared, 0), and exports
`PUBLIC_DRAFT_CONTENT` and `ALPHA_DRAFT_CONTENT` alongside
`ROADMAP_CONTENT` / `LEAK_SUMMARY`.

## Twin-scenario demo (`POST /demo/run`, 8 steps)

`/demo/run` (admin-gated) resets, then runs two same-label twin pairs. It
returns `{"steps": [...8 entries...], "alpha_run_id", "beta_run_id",
"public_a_run_id", "public_b_run_id"}` where `alpha_run_id` / `beta_run_id`
are the A/B alpha runs. Each step is
`{"step", "actor", "action", "expected", "actual", "ok"}` numbered 1..8.

| # | Actor | Action | Expected |
|---|---|---|---|
| 1 | A (public) (public, 0) | read `public-source` via shared_doc | allowed |
| 2 | A (public) | write `PUBLIC_DRAFT_CONTENT` to `public-draft` via shared_doc | allowed |
| 3 | B (public) (public, 0) | read `public-draft` via shared_doc | allowed |
| 4 | B (public) | write the read content to `public-outbox` via shared_kv | allowed |
| 5 | A (alpha) (alpha, 2) | read `alpha-roadmap` via shared_doc | allowed |
| 6 | A (alpha) | write `ALPHA_DRAFT_CONTENT` to `alpha-draft` via shared_doc | allowed |
| 7 | B (alpha) (alpha, 2) | read `alpha-draft` via shared_doc | allowed |
| 8 | B (alpha) | write the draft content to `public-outbox` via shared_kv | blocked `deny-write-down`; B (alpha) NOT suspended; `public-outbox` content unchanged |

## Intentional changes vs the old 5-step table

- The old step 2 (Alpha writes a roadmap leak to `shared-board` after reading
  the roadmap) no longer reaches the scanner: Alpha's effective label is
  `(2, [alpha])` and `shared-board` is `(0, [])`, so it now stops at
  `deny-write-down` **without suspension**.
- Scanner suspension (`deny_and_suspend`) remains in force for writes that
  pass the flow check — e.g. content matching another project's fingerprint
  that the writing run never read through the gateway, so its effective label
  still flows to the destination.
- The old ad-hoc Alpha/Beta 5-step table in `POST /demo/run` is replaced by
  the 8-step twin scenario above; external demo/test mirrors must be updated
  to match.

## Seeded fingerprints, containment rung, and scope honesty

- At startup/reset the gateway seeds fingerprints for every resource with
  sensitivity > 0 and non-empty content (`_ensure_seed_fingerprints`, called
  after each `seed_data` in `create_app`, the startup handler,
  `POST /demo/reset`, and `POST /demo/run`). Seeding is idempotent: a
  fingerprint is inserted (with `first_read_by = "seed"`) only when no
  existing fingerprint has the same `source_resource_id` and hash. Public
  (sensitivity 0) resources are skipped. This closes the pristine-database
  hole where a writer who had read nothing could post protected content
  verbatim before any read had created a fingerprint.
- Below the Jaccard rungs, `scan_write` adds a containment rung: when at
  least 50% of the smaller shingle set (the write's shingles versus the
  fingerprint's shingles) is shared, the write is denied as
  `detect-suspicious-overlap` **without suspension**, even when the Jaccard
  similarity is below 0.15. This catches fragmented copies whose overall
  Jaccard score is diluted by unrelated padding.
- Canary values never appear in event reasons: a canary block names the
  policy and fingerprint, not the leaked token itself. The matched canary
  remains available only as the internal `matched_canary` field of the
  `scan_write` result.
- Scope honesty: flow labels are the guarantee for reads tracked through
  the gateway. The scanner is a backstop, not the principal control. A
  paraphrase with near-zero shingle overlap written by a run that never
  read the source through the gateway remains out of scope; for such
  never-read writers the scanner can only catch verbatim, near-verbatim,
  canary-bearing, or fragmented-but-overlapping copies, and must not be
  described as protecting against arbitrary paraphrase exfiltration.


---

# Tier 2 Amendment — trust boundary (supersedes conflicting text above)

## Admin boundary (fail-closed)

- `create_app(db_path=None, policies_path=None, admin_key=None, demo_mode=None)`;
  `app.state.admin_key` may be `None` (admin disabled) and
  `app.state.demo_mode` records the resolved mode.
- Key resolution: `admin_key` arg > `FLOWGATE_ADMIN_KEY` env >
  `GATEWAY_ADMIN_KEY` env > the well-known dev key `"flowgate-dev-admin"`
  **only** when demo mode is explicitly enabled (`demo_mode=True` arg or
  `FLOWGATE_DEMO_MODE` in {1,true,yes,on}). Otherwise no key is valid:
  admin endpoints return 401 `admin-required`. The dev key outside demo
  mode must NOT authenticate.
- Admin-gated endpoints now also include the inspection GETs:
  `GET /runs`, `GET /resources`, `GET /events`, `GET /graph`,
  `GET /artifacts/{id}/versions`, `GET /tool-receipts`. `GET /health`
  stays open. `POST /tools/{tool_name}` remains run-token authenticated;
  `POST /artifacts` is run-token authenticated (below).
- The frontend must never bundle an admin key (no `VITE_*` admin secret):
  the operator enters it at runtime and it is kept only in
  `sessionStorage` (`flowgate-admin-key`), sent as `X-Admin-Key` on
  dashboard requests.

## Persistence and atomic audit

- Startup seeds fixtures **only when the resources table is empty**
  (`_seed_if_empty`). Restarting against an existing database preserves
  resource contents; `POST /demo/reset` / `POST /demo/run` remain the
  explicit, admin-gated reseed paths.
- Every tool execution and its audit event commit in **one SQLite
  transaction**: allowed writes commit resource mutation + artifact
  version + execution receipt + event together; allowed reads commit
  fingerprint + effective-label join + receipt + event together;
  `deny_and_suspend` commits suspension + event together. A failure
  (e.g. injected event-insert error) rolls the mutation back — a write
  never persists silently without its event. Store primitives used in
  these paths accept `commit=False` for caller-managed transactions.

## Versioned artifacts with inherited labels

- `POST /artifacts` (run bearer token) with
  `{"resource_id", "kind": "doc"|"kv", "content"}` creates a resource at
  version 1. The server assigns the label from the creator run's
  **effective label**: sensitivity = classification,
  compartments = effective compartments (empty ⇒ `shared` public
  artifact; otherwise the resource project is the compartment). Clients
  can never choose the label. Suspended runs get 403 `run-suspended`;
  an existing `resource_id` gets 409 `artifact-exists`.
- Artifact writes through `POST /tools/{tool_name}` append immutable
  versions (`artifact_versions` table; `resources.version` increments).
  `GET /artifacts/{resource_id}/versions` (admin) lists versions with
  their labels. Resources carry `compartments`, `created_by`, `version`;
  `resource_label` honours stored compartments.
- Demo step 6 now creates `alpha-plan-draft` this way after reading
  `alpha-roadmap`, so the twin scenario proves label inheritance for a
  newly created artifact (label `(2, [alpha])`), not only for the
  pre-seeded `alpha-draft`.

## Execution receipts (mediation evidence)

- Allowed tool executions insert an `execution_receipts` row in the same
  transaction and return `execution_receipt_id`. Blocked calls insert
  none. `GET /tool-receipts?run_id=&resource_id=` (admin) returns
  `{"count", "receipts"}`. A denied write is therefore provable as
  "never executed": its receipt count stays at zero while allowed
  writes to the same resource are receipted. Demo step 8 asserts this;
  `scripts/demo.py` re-verifies it externally as step 9.
- Scope honesty: receipts are issued by the in-process tool backend;
  they prove gateway mediation within this deployment. A separately
  deployed tool service holding its own receipt store is future work.

## Tenant label and alerts

- Runs and resources carry a `tenant` label alongside `project`:
  `RunCreate.tenant` is optional and defaults to the run's `project`;
  resources default their tenant to their `project` (seeded fixtures
  and `shared` resources use their project as tenant). Stored artifacts
  inherit the creator run's tenant. Tenant is a hard boundary in
  policy evaluation: when a non-`shared` resource's tenant differs from
  the run's tenant, `project_relation` is `different` (deny
  cross-project applies) even if the project names match. Existing
  single-tenant deployments are unaffected because tenant defaults to
  project everywhere.
- `GET /alerts` (admin) returns `{"count", "alerts"}`: the blocked
  attempts from the event log, newest first, each entry carrying the
  event fields plus the acting run's `project`/`tenant`/`suspended`
  where known. This is the developer alert surface for attempted
  exfiltration and suspensions; the dashboard polls it alongside the
  event feed. Alert/event reasons never contain canary values.

## Creation scan, compartment entitlement, create edges

- Artifact creation runs the same `scan_write` backstop as tool
  writes (target = the inherited label): a fresh run submitting a
  known restricted copy or canary via `POST /artifacts` is blocked
  (`detect-*`, suspension on the suspend rungs) and no resource is
  created. The seeded-fingerprint promise therefore covers every
  content entry point, not only tool writes.
- Reads require compartment entitlement: the resource's compartments
  must be a subset of the run's entitlement — its project plus the
  compartments the controller asserted at run creation (initial
  effective compartments). Otherwise 403 `deny-compartment`, recorded
  as a blocked event, before any content is returned. (Controller-
  asserted initial labels remain a trusted-controller assumption.)
- The lineage graph includes `create` edges (run → resource) so the
  artifact-creation handoff is visible; version-specific provenance
  links are future work.

## Protected tool backend integration

- `backend/tool_service.py` is a separate FastAPI service with its own
  SQLite store and receipts, gated by a service key (`X-Tool-Key`,
  `FLOWGATE_TOOL_SERVICE_KEY`) that only the gateway holds — agent
  runs cannot execute against it directly (401 `tool-key-required`).
- The gateway externalises `public-outbox` when configured
  (`FLOWGATE_TOOL_SERVICE_URL` + key, or `tool_client` injection):
  gateway checks still run first; only allowed calls execute on the
  tool backend, responses carry `tool_receipt_id`, and reads/state
  come from the backend (system of record). A denied call produces no
  backend receipt — independently verifiable via the backend's
  `/receipts` and `/state/{id}` (service key required).
  `scripts/demo.py --tool-service-url/--tool-service-key` checks this
  as step 10. Without configuration the gateway keeps the previous
  in-process execution for the remaining resources.

---

# Tier 3 Amendment — evidence, challenge mode, mini-eval, real-agent step

Additive; supersedes conflicting text above. The deterministic core is
unchanged: `_handle_tool_call` order (suspended → unknown/mismatch →
YAML policy → flow check → scanner → execute), the hardcoded
`evaluate_flow`, thresholds in `detection.py`, and first-match order in
`policies.yaml` must not be altered to make any Tier 3 number look
better.

## Judge-challenge mode (`POST /demo/challenge`)

- New seeded resource `challenge-outbox` (kv, shared, 0): the only
  destination challenge writes may reach, so an allowed challenge write
  never pollutes `public-outbox` or the twin scenario.
- `POST /demo/challenge` is admin-gated (same 401 `admin-required`).
  Body: `{"content": str (1..2000 chars), "writer": "fresh"|"tainted"}`.
  - `fresh`: the gateway creates a disposable run (project `challenge`,
    clearance 0) that has read nothing, then attempts
    `shared_kv write challenge-outbox` with the supplied content through
    the normal `_handle_tool_call` path (flow passes, scanner decides).
  - `tainted`: the gateway creates a disposable alpha run (clearance 2),
    reads `alpha-roadmap` through the gateway (effective label becomes
    `(2, [alpha])`), then attempts the same write (flow check decides).
- Abuse controls: content is capped at 2000 chars by model validation,
  and attempts are rate-limited per app instance (20 per rolling 60s;
  beyond that HTTP 429 `{"policy_id": "challenge-rate-limited"}`). The
  response never returns the submitted content, a canary value, or a
  matched fingerprint identifier.
- Response: `{"decision", "policy_id", "reason", "similarity",
  "suspended", "writer", "layer", "run_id", "event_id"}` where `layer`
  is `"flow"` for `deny-write-down`, `"scanner"` for `detect-*`,
  `"policy"` for other denies, and `"none"` when allowed. `suspended`
  is the challenge run's suspension state after the attempt.
- The dashboard's naive-vs-gated comparison is presentational only: the
  "naive" side is labelled a counterfactual (an ungated shared write
  would have succeeded); the frontend must never perform an ungated
  write to prove it.

## Quantified mini-eval (frozen payload set)

- `scripts/mini_eval.py` runs a frozen set of 24 gateway cases through
  the real HTTP/TestClient path against a temporary database and prints
  the four quotable numbers; `backend/tests/test_mini_eval.py` asserts
  the same outcomes so the numbers cannot silently drift.
  - 12 restricted writes, all expected blocked: 6 by a *tainted* writer
    (alpha run that read `alpha-roadmap`, writing to `public-outbox`;
    exact copy, leak summary, short excerpt, padded copy, clean
    paraphrase, split chunk — expected `deny-write-down`) and 6 by a
    *fresh* writer that never read the source (writing to
    `shared-board`; exact copy, canary-bearing text, high-overlap
    summary, short excerpt, split chunk, padded copy — expected
    `detect-*`).
  - 12 benign public writes by fresh public runs to public
    destinations, all expected allowed (no suspension).
- Reported metrics, always with their bounds: restricted block rate
  with per-layer breakdown (flow vs scanner `policy_id`); benign
  false-positive rate **on those 12 writes only**; scanner-only catch
  rate (direct `scan_write` over the same 12 restricted payloads vs.
  the gateway's 12/12); per-call latency p50/p99 measured around the
  tool calls. Marketing must quote them exactly with these scopes.

## Real-agent step (`scripts/agent_demo.py`)

- External client in the style of `scripts/demo.py` (never imports
  backend code; admin key only for run creation, run token for tool
  calls). It performs ONE real-LLM agent step: a Claude planner with a
  single `gateway_tool` tool proposes calls, the script executes them
  against `POST /tools/{tool_name}`, and the gateway verdict stands —
  the model proposes, the gateway disposes. The model may never set
  labels, choose its run, or override a block; a denied call ends in
  the agent being told the policy reason, not retried around the gate.
- Hard redaction rule: the LLM step runs the **public** task only
  (read `public-source`, publish a summary to `public-outbox`).
  Restricted content (`alpha-roadmap`, drafts derived from it) is
  never placed in a cloud model's context; the alpha climax remains
  the deterministic scripted path. This is the DEMO-08 rule from the
  forward-direction roadmap: same enforcement path, deterministic
  fallback.
- Without `ANTHROPIC_API_KEY` or the optional `anthropic` package, the
  script falls back to a deterministic scripted planner performing the
  identical gateway calls, and says so. Model defaults to
  `claude-opus-5-5` (env `FLOWGATE_AGENT_MODEL` overrides).

## Evidence wall (frontend, no new backend API)

- The dashboard gains an evidence view built only from existing admin
  endpoints (`/events`, `/graph`, `/tool-receipts`, `/alerts`): for the
  latest blocked `deny-write-down` write to `public-outbox` it shows
  (1) the blocked event with `policy_id` and similarity, (2) the
  lineage edge for that attempt (present, marked blocked — the flow
  stopped at the outbox), and (3) execution evidence:
  `/tool-receipts?run_id=<blocked run>&resource_id=public-outbox`
  count, presented as "0 executions for the blocked attempt", next to
  the count of receipted allowed writes to `public-outbox`.
- Copy must state the sole-path assumption: receipt absence proves
  non-execution only where the gateway is the sole path to the tool
  backend (gateway-held tool key, no direct agent access). Event/alert
  reasons never contain canary values (unchanged invariant).

