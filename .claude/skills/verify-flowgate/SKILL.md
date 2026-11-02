---
name: verify-flowgate
description: Run FlowGate's full verification — backend tests (89), frontend typecheck/build, and the 8-step twin demo — and report pass/fail per stage. Use only when invoked with /verify-flowgate.
disable-model-invocation: true
argument-hint: "[full|backend|frontend|demo]"
allowed-tools: Bash PowerShell Read
---

# Verify FlowGate

Target: $ARGUMENTS (default: `full` — run every stage below; otherwise run only the named stage).

Work from the repo root: `${CLAUDE_PROJECT_DIR}`. Windows/PowerShell is the primary shell; the backend runs from `backend/` (import gotcha — see root CLAUDE.md).

## Stage 1 — backend tests (skip unless target is full/backend)

```powershell
cd backend
python -m pytest tests/ -q
```

Expect: 89 passed. On failure, report the failing test names and the first error block verbatim — do not fix anything unless the user asks in a follow-up.

## Stage 2 — frontend build (skip unless target is full/frontend)

The build (`tsc && vite build`) must pass with the backend NOT running — stop any dev backend on port 8000 first if one is running.

```powershell
cd frontend
npm run build
```

Expect: `tsc` clean, vite build succeeds.

## Stage 3 — twin demo (skip unless target is full/demo)

Backend must be running in demo mode for this stage (start it in a separate terminal if needed: `cd backend; $env:FLOWGATE_DEMO_MODE="1"; python -m uvicorn app.main:app --port 8000`), then from the repo root:

```powershell
python scripts/demo.py --admin-key flowgate-dev-admin
```

Check steps 1–8 against the oracle in root CLAUDE.md ("The demo is the test oracle"): public twin steps 1–4 all allowed; alpha step 6 creates `alpha-plan-draft` with inherited label (2, [alpha]); step 8 blocked `deny-write-down`, **not suspended**, `public-outbox` unchanged; external step 9 confirms the denied write produced no execution receipt.

## Report

One line per stage — `backend: PASS (89 passed)` / `frontend: PASS` / `demo: PASS (9/9)` — then details only for failures. End with an overall verdict: ready to demo, or the first thing to fix.
