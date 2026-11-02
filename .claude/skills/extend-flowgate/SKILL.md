---
name: extend-flowgate
description: Extend FlowGate safely — add a tool, resource, policy rule, or demo step while keeping CONTRACT.md, POST /demo/run, scripts/demo.py, and test_gateway_e2e.py in sync. Use when adding or changing gateway behaviour, tools, resources, policies, or demo steps.
allowed-tools: Read Write Edit Bash PowerShell Grep Glob
---

# Extend FlowGate

Before writing any code, read the Tier 1 amendment in [CONTRACT.md](../../../CONTRACT.md) — it supersedes the original 5-step semantics. Semantics change in CONTRACT.md (and its tests) first, code second; never change semantics in code alone.

## Invariants — never break these

- `_handle_tool_call` check order is load-bearing (suspended → unknown/mismatch → policy → flow check → scanner → execute). Do not reorder.
- The flow check (`evaluate_flow`) is hardcoded after the YAML policy. It is **not** a YAML rule and must not become configurable.
- Detection ([detection.py](../../../backend/app/detection.py)) stays pure: no I/O, no app imports.
- Every endpoint touching `state.conn` holds the shared `threading.Lock`.
- Event reasons never contain a canary value (the event feed is unauthenticated).

## By kind of change

- **New tool:** extend `TOOL_KIND` in [tools.py](../../../backend/app/tools.py); the kind check (`shared_doc`→`doc`, `shared_kv`→`kv`) follows automatically. Resources declare `kind` at seed time.
- **New resource:** add it in [seed.py](../../../backend/seed.py) with `project` + `sensitivity` (0–2). Sensitivity > 0 resources are fingerprinted at startup/reset automatically. Keep the exported fixture constants (`ROADMAP_CONTENT`, `LEAK_SUMMARY`, Tier 1 fixture texts) intact — tests import them.
- **New policy rule:** edit [policies.yaml](../../../backend/policies.yaml) only. Rules are first-match-wins in file order; supported `when` keys are `operation`, `project_relation` (`shared`/`same`/`different`), `clearance_vs_sensitivity` (`lt`/`gte`). Suspension on policy deny requires action `deny_and_suspend`.
- **Gateway behaviour / demo step change:** update all three implementations of the twin scenario in sync — `POST /demo/run` in [main.py](../../../backend/app/main.py), [scripts/demo.py](../../../scripts/demo.py) (external HTTP client; admin key only for reset/run creation, run tokens for tool calls; never imports backend code), and [test_gateway_e2e.py](../../../backend/tests/test_gateway_e2e.py).

## Appropriate subagents

Delegate to the project agents in [.claude/agents/](../../agents/): `flowgate-backend-core`, `flowgate-detection`, `flowgate-tools-seed`, `flowgate-frontend`, `flowgate-demo-tester`; use `flowgate-integration` last to wire and smoke-test, and `flowgate-adversarial` to attack the result.

## Finish

Run `/verify-flowgate` (or the equivalent commands) and report per-stage results. A change is not done until the backend tests, the frontend build, and the twin demo all pass.
