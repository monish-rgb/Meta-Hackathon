---
name: attack-flowgate
description: Adversarially test the local FlowGate demo for information leaks — suspended-token reuse, cross-project reads, exact/canary/near-copy leaks, tool-kind mismatch, and false positives — and report findings. Use only when invoked with /attack-flowgate. Local demo only; never fix anything.
disable-model-invocation: true
argument-hint: "[base-url]"
allowed-tools: Bash PowerShell Read
---

# Attack FlowGate (local demo only)

Target: $ARGUMENTS (default base URL: `http://localhost:8000`). Only ever attack the local demo instance. This skill **reports findings and never fixes anything** — fixes go through `/extend-flowgate`.

Prefer delegating the run to the `flowgate-adversarial` subagent (same mandate, isolated context); otherwise work through the checklist directly. The backend must be running; reset state first (`POST /demo/reset` with the admin key, dev default `flowgate-dev-admin`).

## Attack checklist

For each attack: record the request, the gateway's `policy_id`/reason, and whether state changed.

1. **Suspended-token reuse** — get a run suspended (e.g. exact-copy leak), then reuse its token on a benign read. Expect: blocked `run-suspended`, checked before policy.
2. **Cross-project reads** — alpha run reads a beta resource (and vice versa) at every clearance 0–2. Expect: policy deny; no fingerprint/content returned.
3. **Exact copy** — read `alpha-roadmap`, write it verbatim to a lower/cross destination where the flow check permits. Expect: `detect-exact-copy`, deny + suspend.
4. **Canary leak** — include `CANARY-ALPHA-NOVA-7F3K9` in a write. Expect: `detect-canary`, deny + suspend. (Never print the canary value in findings beyond naming this test — event reasons must not contain it; flag it if one does.)
5. **Near-copy ladder** — writes tuned around the thresholds: Jaccard ≥ 0.30 (`detect-taint-overlap`, deny + suspend), 0.15–0.30 (`detect-suspicious-overlap`, deny only), and a short excerpt with containment ≥ 0.50 but Jaccard < 0.15 (deny only). Verify each lands on the expected rung.
6. **Flow-check bypass attempts** — a run that read `alpha-roadmap` (effective label `(2, {alpha})`) writes an original, zero-overlap paraphrase to `public-outbox`. Expect: blocked `deny-write-down`, **no suspension**, outbox unchanged — the flow check, not the scanner, must stop it.
7. **Tool-kind mismatch** — call `shared_doc` on a kv resource and `shared_kv` on a doc. Expect: `tool-resource-mismatch`. Plus unknown tool/resource probes (`unknown-tool`, `resource-not-found`).
8. **Admin gating** — `POST /runs`, suspend/reactivate, `POST /demo/reset`, `POST /demo/run` with no key and with a wrong key. Expect: 401 `admin-required`, and no event recorded.
9. **False positives** — the public twin flow (read `public-source` → write `public-draft` → read → publish to `public-outbox`) and clean same-project writes. Expect: all allowed; sensitivity-0 fingerprints never trigger the scanner.
10. **Known-default admin key** — against a backend started *without* an admin key and without demo mode, try creating a run with the well-known dev key. Expect: 401 `admin-required` (fail-closed); only an explicitly demo-mode backend accepts it.
11. **Unauthenticated inspection** — `GET /runs`, `/resources`, `/events`, `/graph`, `/tool-receipts` with no key. Expect: 401 on all (only `/health` is open).
12. **Restart persistence** — write to `public-outbox`, restart the backend on the same database, read the outbox back. Expect: content preserved; startup must not reseed over existing resources.
13. **Artifact label inheritance** — a run that read `alpha-roadmap` creates an artifact via `POST /artifacts`. Expect: 201, server-assigned label `(2, {alpha})`, version 1; duplicate create 409; a subsequent tool write appends version 2 (`GET /artifacts/{id}/versions`, admin).
14. **Execution receipts** — after a denied write-down, query `GET /tool-receipts` (admin) for that run + `public-outbox`. Expect: zero write receipts for the denied call; allowed writes carry `execution_receipt_id`.

## Report

A table: attack → expected → actual `policy_id` → verdict (holds / **finding**). For every finding: exact reproduction steps and the smallest evidence (response body, event entry). State plainly what held. Out of scope by design (do not report as findings): paraphrase with ~0 shingle overlap by a writer who never read through the gateway, and channels outside the gateway.
