# FlowGate — Demo & Testing Guide

Step-by-step instructions to test the repository and run the live demo. All commands are PowerShell (Windows).

Repo root used below: `C:\Users\MONISH\OneDrive\Desktop\Meta Hackathon`

---

## Step 0 — Prerequisites

```powershell
python --version    # 3.12+
node --version      # 18+
cd "C:\Users\MONISH\OneDrive\Desktop\Meta Hackathon"
```

---

## Step 1 — Automated backend tests (fastest signal)

```powershell
cd backend
python -m pip install -r requirements.txt
python -m pytest tests/ -q
```

**Expect:** `89 passed`. This covers the detection ladder + containment rung, the policy/flow evaluator, and the Tier 1+2 acceptance suite (admin gating incl. fail-closed/demo-mode, twin scenario with artifact creation, restricted payloads, public no-false-positive, restart persistence, atomic write/audit rollback, artifact versions, execution receipts).

Optional, per layer:

```powershell
python -m pytest tests/test_detection.py -q     # scanner units
python -m pytest tests/test_policy.py -q        # policy + flow labels
python -m pytest tests/test_gateway_e2e.py -q   # end-to-end acceptance
```

---

## Step 2 — Frontend build check

```powershell
cd ..\frontend
npm install
npm run build
```

**Expect:** `tsc && vite build` completes with no errors. The backend does *not* need to be running for the build.

---

## Step 3 — Live end-to-end demo (external HTTP client)

### Terminal 1 — start the backend

Use a throwaway database so you don't touch your real state:

```powershell
Set-Location "C:\Users\MONISH\OneDrive\Desktop\Meta Hackathon\backend"
Get-Location
Test-Path .\app\main.py        # must print True
$env:GATEWAY_DB = "$env:TEMP\flowgate-test.db"
$env:FLOWGATE_DEMO_MODE = "1"
Remove-Item $env:GATEWAY_DB -ErrorAction SilentlyContinue
python -m uvicorn app.main:app --port 8000
```

Wait for `Uvicorn running on http://127.0.0.1:8000`.

> **Important:** the backend must be launched from the `backend\` directory — `app` is the package inside it. Environment variables (`$env:GATEWAY_DB`, `$env:FLOWGATE_ADMIN_KEY`) only live in the terminal where you set them, and each new terminal starts fresh.

### Terminal 2 — sanity check + run the demo

```powershell
curl.exe http://localhost:8000/health
```

**Expect:** `{"status":"ok"}`

```powershell
cd "C:\Users\MONISH\OneDrive\Desktop\Meta Hackathon"
python scripts/demo.py --admin-key flowgate-dev-admin
```

**Expect:** steps 1–9 all `PASS`, ending with `9/9 steps passed`, exit code 0.

The key line is **step 8**: the second alpha run — which only read the *draft*, never the roadmap — is blocked with `deny-write-down`, is **not** suspended, and `public-outbox` still holds the public text. **Step 9** externally verifies the denied write produced no tool execution receipt, while the allowed publish did.

### Optional — separate protected tool backend (step 10)

Prove the denied write never reaches an *independent* execution backend. Terminal A — tool backend (its own DB + service key; agents can't call it directly):

```powershell
cd backend
$env:FLOWGATE_TOOL_SERVICE_KEY = "tool-secret-123"
$env:FLOWGATE_TOOL_DB = "$env:TEMP\flowgate-tools.db"
python -m uvicorn tool_service:app --port 8001
```

Restart the gateway (Terminal 1) wired to it: add `$env:FLOWGATE_TOOL_SERVICE_URL = "http://127.0.0.1:8001"` and `$env:FLOWGATE_TOOL_SERVICE_KEY = "tool-secret-123"` before launching. Then:

```powershell
python scripts/demo.py --admin-key flowgate-dev-admin --tool-service-url http://127.0.0.1:8001 --tool-service-key tool-secret-123
```

**Expect:** 10/10 — step 10 confirms the tool backend's own receipts show the allowed publication, zero denied writes, and its state holds only the public content. Try `POST http://127.0.0.1:8001/execute` without `X-Tool-Key` → `401 tool-key-required` (agents cannot bypass the gateway).

### What the 8 steps prove

| Steps | Scenario | Expected |
|---|---|---|
| 1–4 | Public collaboration: A reads `public-source` → writes `public-draft` → B reads it → publishes to `public-outbox` | All allowed, content lands |
| 5–7 | Restricted collaboration: A reads `alpha-roadmap` → **creates** `alpha-plan-draft` (server assigns the inherited secret/alpha label, version 1) → B reads only the draft | All allowed internally; B's effective label becomes `secret {alpha}` |
| 8 | B tries to publish the draft to `public-outbox` | Blocked `deny-write-down`, not suspended, outbox unchanged, no execution receipt |

---

## Step 4 — Dashboard test

### Terminal 3 — start the frontend

```powershell
cd "C:\Users\MONISH\OneDrive\Desktop\Meta Hackathon\frontend"
npm run dev
```

Open **http://localhost:5173**, then:

0. Paste the admin key (`flowgate-dev-admin` in demo mode) into the header key field and click **Save key** — it is kept only in this browser session; the dashboard data loads only with a valid key.
1. Click **Run Flow Demo** → Simulator panel shows **Passed 8/8 steps**.
2. In **Runs**, check the four runs: the two public runs show `context: public`; both alpha runs show `context: secret {alpha}` — including the second alpha run, which only read the draft.
3. In **Events**, find the blocked publish: `deny-write-down` in red, with a reason naming the effective and destination labels.
4. In the **Lineage Graph**, trace `alpha-roadmap → alpha run → alpha-plan-draft → alpha run → ✗ public-outbox`.
5. Click **Reset** → everything clears and reseeds.

---

## Step 5 — Manual security probes (optional but convincing)

With the backend from Step 3 still running, in a new terminal:

### No admin key → can't create a privileged run

```powershell
curl.exe -i -X POST http://localhost:8000/runs -H "Content-Type: application/json" -d '{\"project\":\"alpha\",\"clearance\":2}'
```

**Expect:** `401` with `"policy_id":"admin-required"`.

### With the (demo-mode) admin key → works

```powershell
curl.exe -X POST http://localhost:8000/runs -H "Content-Type: application/json" -H "X-Admin-Key: flowgate-dev-admin" -d '{\"project\":\"alpha\",\"clearance\":2}'
```

**Expect:** `201` with a `run_id` and `token`. Copy the token, then try using it as an admin key (should fail), and confirm a run can't reactivate itself after suspension without the admin key.

### Unauthenticated admin endpoints

```powershell
curl.exe -i -X POST http://localhost:8000/demo/reset
```

**Expect:** `401 admin-required` — the audit history survives.

### Tool call without a run token

```powershell
curl.exe -i -X POST http://localhost:8000/tools/shared_doc -H "Content-Type: application/json" -d '{\"operation\":\"read\",\"resource_id\":\"alpha-roadmap\"}'
```

**Expect:** `401` with `"policy_id":"auth-failed"`.

---

## Step 6 — Custom admin key (non-local setup)

Restart the backend (Terminal 1) with your own key:

```powershell
$env:FLOWGATE_ADMIN_KEY = "pick-a-real-secret"
python -m uvicorn app.main:app --port 8000
```

The demo client needs the matching key:

```powershell
python scripts/demo.py --admin-key pick-a-real-secret
```

The dashboard needs no restart or rebuild: paste the same key into the header key field and click **Save key** (kept only in `sessionStorage`; **Clear** removes it). The key is never bundled into the frontend.

Without a valid key, dashboard data and **Run Flow Demo / Reset / Suspend** will fail with 401 — that's the gating working, not a bug.

Key resolution order (backend): `create_app(admin_key=...)` argument → `FLOWGATE_ADMIN_KEY` → `GATEWAY_ADMIN_KEY` → `"flowgate-dev-admin"` **only when demo mode is explicitly enabled** (`FLOWGATE_DEMO_MODE=1`). Otherwise the backend is fail-closed.

---

## Success checklist

| Check | Expected |
|---|---|
| `python -m pytest tests/ -q` | 89 passed |
| `npm run build` | clean, backend down |
| `GET /health` | `{"status":"ok"}` |
| `python scripts/demo.py --admin-key flowgate-dev-admin` | 9/9 PASS, exit 0 |
| Dashboard demo | 8/8 steps; alpha runs show `context: secret {alpha}` |
| `POST /runs` without key | 401 `admin-required` |
| `POST /tools/...` without token | 401 `auth-failed` |
| Blocked publish event | `deny-write-down`, run not suspended, outbox unchanged |

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'app'`

The backend was launched from the wrong directory. `app` is the package inside `backend\`, so `backend\` must be the working directory:

```powershell
Set-Location "C:\Users\MONISH\OneDrive\Desktop\Meta Hackathon\backend"
Test-Path .\app\main.py        # must print True
python -m uvicorn app.main:app --port 8000
```

Common causes: a brand-new terminal (starts in your home directory), or running from `frontend\` after Step 2. The import fails before anything runs, so your code and database are untouched — just relaunch from the right place.

Directory-independent alternative, works from anywhere:

```powershell
python -m uvicorn app.main:app --app-dir "C:\Users\MONISH\OneDrive\Desktop\Meta Hackathon\backend" --port 8000
```

### Port 8000 / 5173 already in use

A previous server is still running. Stop it with `Ctrl+C` in its terminal, or find and close the process, then relaunch.

### Demo steps fail with 401

Admin-key mismatch between the backend (`FLOWGATE_ADMIN_KEY`, or demo mode) and its clients: the demo script needs `--admin-key` / `FLOWGATE_ADMIN_KEY`, and the dashboard needs the same key pasted into the header field. There is no built-in fallback outside explicit demo mode.

### Weird leftover state

You are probably using the real `backend\gateway.db`. Either use the temp `GATEWAY_DB` from Step 3, or click **Reset** in the dashboard. Don't delete `gateway.db` while the server is running.

### Frontend says "Backend unreachable"

The backend terminal isn't running (or crashed). Check:

```powershell
curl.exe http://localhost:8000/health
```

### Tests fail with import errors

Run pytest from `backend\`:

```powershell
cd "C:\Users\MONISH\OneDrive\Desktop\Meta Hackathon\backend"
python -m pytest tests/ -q
```

---

## Presentation script (3 minutes)

1. **0:00–0:30** — The problem: agent runs share tools; a run can read confidential data, hand a derived draft to another run, and the second run publishes it. Per-call permissions allow every step.
2. **0:30–1:10** — Scenario A (Steps 1–4): public collaboration works end to end and the content really lands in `public-outbox`.
3. **1:10–2:10** — Scenario B (Steps 5–8): the confidential draft is a paraphrase with **no canary** and almost no textual overlap — a content scanner waves it through. It is blocked anyway, because the second run *inherited the label* from the draft it read.
4. **2:10–2:40** — Evidence: the blocked event (`deny-write-down`), the destination unchanged, the run **not** suspended (it can keep doing alpha work — only the public flow is denied).
5. **2:40–3:00** — The honest scope: after a secret read, public output from that same context is denied; public work needs a genuinely clean session. That's the model, stated up front.
