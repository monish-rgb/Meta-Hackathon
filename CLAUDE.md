# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

FlowGate is an externally enforced information-flow gateway between AI agent runs and shared tools (`shared_doc`, `shared_kv`). Runs and resources carry labels (`project` + `clearance`/`sensitivity` 0–2); the gateway checks policy *before* executing a tool call, fingerprints everything a run reads, scans everything it writes against other projects' fingerprints, and blocks/suspends on leaks. Every attempt lands in an append-only SQLite event log rendered as a live lineage graph.

[CONTRACT.md](CONTRACT.md) is the contract all components were built against — API shapes, label semantics, policy/detection behaviour, and the demo tables. It carries a **Tier 1 amendment** (admin gating, effective context labels, flow check, twin-scenario demo) that supersedes the original 5-step semantics where they conflict. Treat it as the source of truth: don't change semantics in code without updating it (and the tests that encode it).

## Commands

All backend commands run from [backend/](backend/) (see the import gotcha below); frontend commands from [frontend/](frontend/). Windows/PowerShell is the primary shell.

```powershell
# Backend (Python 3.12+)
cd backend
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000        # http://localhost:8000, docs at /docs

# Tests — 89 tests across detection, policy, gateway e2e (Tier 1+2 acceptance)
python -m pytest tests/ -q
python -m pytest tests/test_detection.py -q                          # single file
python -m pytest tests/test_gateway_e2e.py -q                        # Tier 1 acceptance suite
python -m pytest tests/ -k policy -q                                 # by keyword

# Frontend (Node 18+)
cd frontend
npm install
npm run dev        # http://localhost:5173
npm run build      # tsc && vite build — must pass with the backend NOT running

# Headless demo (backend must be running, in demo mode or with a configured key) — twin public/restricted scenario over HTTP
python scripts/demo.py --admin-key flowgate-dev-admin   # from repo root; --base-url to override
```

There is no linter/formatter configured. The frontend typecheck is `npm run build` (it runs `tsc`).

## Architecture

### Request path — [backend/app/main.py](backend/app/main.py)

`create_app(db_path=None, policies_path=None, admin_key=None, demo_mode=None)` is the app factory (module-level `app = create_app()`); tests instantiate it with a tmp DB. A single shared SQLite connection is serialised by `threading.Lock` around every endpoint — don't add endpoints that touch `state.conn` without the lock.

**Admin gating (Tier 1, hardened Tier 2):** `POST /runs`, suspend/reactivate, `POST /demo/reset`, `POST /demo/run`, and all inspection GETs (`/runs`, `/resources`, `/events`, `/graph`, `/artifacts/*/versions`, `/tool-receipts`) require the admin key (`X-Admin-Key` header or Bearer admin key) and return 401 `admin-required` without it. Key resolution is fail-closed: `admin_key` arg > `FLOWGATE_ADMIN_KEY` env > `GATEWAY_ADMIN_KEY` env > `"flowgate-dev-admin"` **only** when demo mode is explicitly enabled (`demo_mode` arg / `FLOWGATE_DEMO_MODE`); otherwise no key is valid. Only the controller creates runs, so `RunCreate`'s initial `effective_level`/`effective_compartments` are trusted. `/health` stays open; tool calls use run tokens, never the admin key.

All tool calls funnel through `_handle_tool_call`, shared by `POST /tools/{tool_name}` and `POST /demo/run`. Its check order is load-bearing and must not be reordered:

1. **Suspended run** → blocked `run-suspended` (before policy, per contract)
2. Unknown tool → `unknown-tool`; unknown resource → `resource-not-found`; tool≠resource kind (`TOOL_KIND`: `shared_doc`→`doc`, `shared_kv`→`kv`) → `tool-resource-mismatch`
3. **Policy** ([backend/app/policy.py](backend/app/policy.py) + [backend/policies.yaml](backend/policies.yaml)) — first-match-wins over YAML rules using only two derived facts: `project_relation` (`shared`/`same`/`different`) and `clearance_vs_sensitivity` (`lt`/`gte`). No match → `default-deny`.
4. **Writes only — flow check** (`evaluate_flow`, Tier 1): the run's *effective context label* `(classification, compartments)` must flow to the destination label (`eff.class ≤ dest.class` and `eff.compartments ⊆ dest.compartments`), else blocked `deny-write-down` with **no suspension**. Hardcoded after the YAML policy so an early allow rule can never bypass it. The effective label joins (`max`, `∪`) the source label of everything the run has read — it is the principal confidentiality control; the scanner below is a backstop.
5. **Writes only:** `scan_write` against *all* stored fingerprints. Deny/suspend per the ladder below.
6. **Execute** via [backend/app/tools.py](backend/app/tools.py) (assumes already authorised; raises `ValueError` on unknown tool/operation), then record the event. Allowed **reads store a fingerprint** of the returned content *and* join the source resource's label into the run's effective label before returning — this is what makes later cross-run blocking possible.

Auth failures (401 `auth-failed`) and admin failures (401 `admin-required`) are the only blocked calls *not* recorded as events, since no run is known.

### Detection — [backend/app/detection.py](backend/app/detection.py)

Pure functions, no I/O, no app imports. `scan_write` ignores sensitivity-0 (public) fingerprints entirely; other fingerprints are considered only where `source_sensitivity > target_sensitivity` **or** (`source_project != run_project` and `source_sensitivity > 0`). It then applies a severity ladder in order:

| Match | policy_id | Action |
|---|---|---|
| SHA-256 of normalised content | `detect-exact-copy` | deny + suspend |
| Canary token (`CANARY-[A-Z0-9-]+`) intersection | `detect-canary` | deny + suspend |
| Word 5-shingle Jaccard ≥ 0.30 (`SUSPEND_THRESHOLD`) | `detect-taint-overlap` | deny + suspend |
| Jaccard ≥ 0.15 (`DENY_THRESHOLD`) | `detect-suspicious-overlap` | deny only |
| Shingle containment ≥ 0.50 (`CONTAINMENT_THRESHOLD`) | `detect-suspicious-overlap` | deny only |
| otherwise | `detect-clean` | allow |

Containment = `|write ∩ fingerprint| / min(|write|, |fingerprint|)` over 5-shingles; it catches short excerpts, split chunks, and padded copies whose whole-document Jaccard stays under 0.15. The returned `similarity` stays the max Jaccard (containment is reported in the reason). `matched_canary` remains an internal field — event reasons never contain the canary value, since the event feed is unauthenticated.

Detection is deliberately deterministic/explainable — paraphrase-level and out-of-gateway channels are out of scope (see README "Scope"). The flow check, not the scanner, is the guarantee for anything read through the gateway; the scanner is the backstop for flow-compatible writers. Every block names its `policy_id` and, for overlaps, the similarity score.

### Store & models

- [backend/app/store.py](backend/app/store.py) — SQLite (`runs`, `resources`, `events`, `fingerprints`); all functions take `conn` first. Run dicts expose `token` **only** at creation, and carry `effective_level`/`effective_compartments` (the run's joined context label; `init_db` migrates existing DBs, and `update_run_effective_label` joins + persists). `reset_all` clears runs/events/fingerprints but not resources (the caller reseeds).
- [backend/app/models.py](backend/app/models.py) — `RunCreate{project, clearance 0..2, effective_level?, effective_compartments?}`, `ToolCallRequest{operation: read|write, resource_id, content?}`.
- [backend/app/auth.py](backend/app/auth.py) — bearer run tokens (`secrets.token_urlsafe(32)`); suspension is the revocation mechanism. Also the admin-key check (`X-Admin-Key` or Bearer admin key, `secrets.compare_digest`).
- [backend/seed.py](backend/seed.py) — nine demo resources: the original five plus `alpha-draft` (alpha, 2), `public-source`/`public-draft` (shared, 0), `public-outbox` (kv, shared, 0). `alpha-roadmap` (alpha, sensitivity 2) embeds canary `CANARY-ALPHA-NOVA-7F3K9`. Exports include `ROADMAP_CONTENT`/`LEAK_SUMMARY` (imported by tests — keep them) and the Tier 1 fixture texts `PUBLIC_SOURCE_CONTENT`, `PUBLIC_DRAFT_CONTENT`, `ALPHA_DRAFT_CONTENT` (short no-canary roadmap paraphrase, ~0 shingle overlap). Sensitivity->0 resources are fingerprinted at startup/reset (`_ensure_seed_fingerprints`), so a pristine DB is never fingerprint-free.

### Frontend

See [frontend/CLAUDE.md](frontend/CLAUDE.md) (loads only when working under [frontend/](frontend/)).

## The demo is the test oracle

The flagship is now the 8-step **twin scenario** (Tier 1, artifact step Tier 2): public A reads `public-source` → writes `public-draft` → public B reads it → publishes to `public-outbox` (all allowed); then alpha A reads `alpha-roadmap` → **creates `alpha-plan-draft`** via `POST /artifacts` (server assigns inherited label `(2, {alpha})`, version 1) → alpha B reads *only the created draft* (inheriting effective label `(2, {alpha})`) → its publish to `public-outbox` is blocked `deny-write-down`, not suspended, outbox unchanged, and produces no execution receipt. It is implemented three times and must stay in sync: `POST /demo/run` in [main.py](backend/app/main.py), [scripts/demo.py](scripts/demo.py) (external HTTP client — never imports backend code; admin key only for reset/run creation, run tokens for tool calls), and [test_gateway_e2e.py](backend/tests/test_gateway_e2e.py) (the Tier 1 acceptance suite: admin gating, twin scenario, parameterized restricted payloads, public no-false-positive). If you change gateway behaviour, all three are affected. The pre-Tier-1 5-step table is superseded (see the CONTRACT.md amendment) — e.g. a tainted write to `shared-board` now stops at `deny-write-down` before the scanner, without suspension.

## Gotchas

- **Run the backend from `backend/`.** [main.py](backend/app/main.py) mixes `from seed import seed_data` with `from app.… import …`; both only resolve with `backend/` as cwd/`sys.path`. The e2e test inserts `backend/` into `sys.path` itself, so pytest also works from the repo root.
- **State lives in [backend/gateway.db](backend/gateway.db)** (override with `GATEWAY_DB`). It's seeded on startup; use `POST /demo/reset` (or the dashboard Reset button) for a clean slate instead of deleting the file while the server runs.
- **Adding a policy rule:** edit [policies.yaml](backend/policies.yaml) only — rules are evaluated in file order, first match wins, and supported `when` keys are `operation`, `project_relation`, `clearance_vs_sensitivity`. Suspension on policy deny requires action `deny_and_suspend`; only detection currently emits it. The flow check is **not** a YAML rule — it runs after policy-allow in `_handle_tool_call` and cannot be configured away.
- **Admin key:** the backend is fail-closed (Tier 2): set `FLOWGATE_ADMIN_KEY`, or explicitly enable demo mode (`FLOWGATE_DEMO_MODE=1`, dev key `"flowgate-dev-admin"`) for local demos — with neither, admin endpoints return 401. The frontend never bundles a key; the operator enters it at runtime (kept only in `sessionStorage`). All GET inspection endpoints (`/runs`, `/resources`, `/events`, `/graph`, `/artifacts/*/versions`, `/tool-receipts`) require the admin key; `/health` is open.
- **Persistence/atomicity (Tier 2):** startup seeds only an empty resources table — restarts preserve content; explicit `/demo/reset` reseeds. Tool execution + artifact version + execution receipt + event commit in one transaction (store primitives take `commit=False`); a failed event insert rolls the write back.
- **Artifacts (Tier 2):** `POST /artifacts` (run token) creates a versioned resource whose label is server-assigned from the creator's effective label; tool writes append versions. The twin demo step 6 creates `alpha-plan-draft` this way. Allowed executions return `execution_receipt_id`; denied calls produce no receipt (`GET /tool-receipts`, admin).
- **Tenant & alerts (goal completion):** runs/resources carry a `tenant` label defaulting to `project`; a cross-tenant access is `project_relation: different` in policy evaluation even when project names match, and artifacts inherit the creator's tenant. `GET /alerts` (admin) returns blocked attempts with run project/tenant context — the developer alert surface the dashboard polls.
- **Creation scan & compartment reads:** `POST /artifacts` runs `scan_write` against the inherited label (fresh-run restricted copies/canaries are blocked + suspended, nothing created). Reads additionally require the resource's compartments ⊆ run entitlement (project + controller-asserted initial effective compartments), else 403 `deny-compartment`. Graph includes `create` edges.
- **Protected tool backend:** [backend/tool_service.py](backend/tool_service.py) is a separate service (own DB + receipts, `X-Tool-Key` only the gateway holds). With `FLOWGATE_TOOL_SERVICE_URL`/`FLOWGATE_TOOL_SERVICE_KEY` set, `public-outbox` executes there after gateway checks; denied calls leave no backend receipt (demo step 10 verifies independently).
- **Adding a tool:** extend `TOOL_KIND` in [tools.py](backend/app/tools.py) and the kind check follows automatically; resources declare their `kind` (`doc`/`kv`) at seed time.
- No Cursor/Copilot rules exist in this repo. Project subagents for this codebase live in [.claude/agents/](.claude/agents/) (backend-core, detection, tools-seed, frontend, demo-tester, integration, adversarial verifier). Project skills live in [.claude/skills/](.claude/skills/): `/verify-flowgate` (tests + build + twin demo), `/extend-flowgate` (safe-change checklist; Claude may also load it automatically), `/attack-flowgate` (local adversarial leak checklist).
