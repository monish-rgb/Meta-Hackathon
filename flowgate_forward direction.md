# FlowGate: Hackathon Improvement Roadmap and Acceptance Tests

Prepared: October 28, 2026

**Status:** Proposed direction and acceptance criteria. This document does not implement changes, amend `CONTRACT.md`, or certify that the proposed tests pass. It is intentionally saved outside the repository.

## 1. Recommendation

Continue FlowGate as a focused information-flow enforcement project. The current folder provides a useful starting point: policy checks happen before tool execution, the detector is easy to inspect, and an end-to-end demo exists. The next milestone should establish a trustworthy security boundary and prove it with independent backend evidence.

The broad proposition, "a security gateway for AI agents," overlaps with existing products. The more specific proposition worth demonstrating is:

> Confidentiality restrictions should survive agent reads, derived artifacts, and handoffs between runs. A protected publication must be stopped before execution when its destination cannot accept that information.

The strongest hackathon result is one complete, reproducible workflow with a clear security guarantee. More services, more agents, and more dashboard features can wait.

## 2. Main problem statement and goal

### Target user

A platform or engineering team running agents for multiple projects or clients, with shared document, artifact, or cache services. Individual runs have different permissions, while some collaboration is legitimate.

### Problem statement

> An agent can legitimately read confidential project data and create a derived artifact that another agent can access. If confidentiality restrictions are lost during that handoff, the second agent can publish information to an unauthorized destination. Static permissions on individual calls may allow each step without accounting for the information the workflow has already consumed. Developers need enforceable sharing rules across runs and evidence of which transfers were allowed or stopped.

### Main goal

**Prevent unauthorized publication through the protected artifact service by maintaining trusted confidentiality state across runs and artifact versions, while allowing the equivalent public-information workflow.**

For the demonstration, prove all of the following:

1. Two separately identified agent clients can collaborate on a confidential internal draft.
2. Restrictions propagate from the original document to the draft and then to the second run.
3. Public publication from the restricted context is denied before the backend publication operation is dispatched.
4. The public destination remains unchanged and has no publication dispatch receipt for the denied request.
5. A clean public workflow produces a real publication and a backend receipt.
6. The policy decision remains correct when text is shortened, paraphrased, encoded, or stripped of canaries, provided the information entered the tracked context.

The sixth condition is enforced through conservative context labels. It does not require the gateway to recognize the meaning of the submitted text.

### Suggested pitch

> FlowGate carries confidentiality restrictions through agent handoffs and blocks unauthorized publication through protected shared tools, with a verifiable record of what reached the backend.

### Claims to avoid

- Universal prevention of data leakage, prompt injection, collusion, or sandbox escape.
- Semantic understanding of every summary or paraphrase.
- Proof that a particular output sentence came from a particular input document.
- Worker termination when only gateway access was revoked.
- Protection of shell, HTTP, files, or external tools that remain outside the enforced boundary.
- "Only a cross-run view can detect this." Some attacks are visible within one run.

## 3. Current evidence and gaps

These are results from the repository review earlier on October 28, 2026. They were not rerun merely to produce this document.

| Area | Observed result | Meaning |
|---|---|---|
| Existing backend suite | 48 tests passed using Python 3.13 | Existing contract behavior is covered; this does not validate the stronger design proposed here. |
| Frontend | Production build passed | The frontend compiles; browser usability and deployment were not certified. |
| Five-step demo | All steps passed through FastAPI TestClient | The scripted example works with simulated clients. |
| Exact-copy denial | HTTP 403; destination unchanged | The recognized-copy blocking path has a real effect on protected state. |
| Run provisioning | A client could choose project `alpha`, clearance `2`, and read the secret roadmap | Run identity and privileges are not assigned exclusively by a trusted controller. |
| Administrative controls | Suspension/reactivation endpoints accepted unauthenticated requests | A reachable client can undo suspension. |
| Audit reset | Unauthenticated `/demo/reset` removed events | Demo controls and security records need separate protection. |
| Short leak | A verbatim launch-date excerpt was written to the shared board and read by Beta | Whole-document similarity misses ordinary sensitive excerpts. |
| Padded leak | A longer copy with the canary removed and padding added was written and read by Beta | Content matching is insufficient as the primary confidentiality guarantee. |
| Public sharing | Reading a public note, then copying it to another public resource, caused suspension | The detector treats the `shared` project as foreign even for public content. |
| Provenance | Graph edges represent tool attempts; structured source-match references and resource versions are incomplete | The current graph does not establish the full chain promised by the proposed product. |

The HTTP adversarial checks used isolated in-memory databases. They were not real model runs, separate sandboxed agent processes, or a deployed containment test. The original HTTP test stall was traced to sandbox loopback setup; the full suite passed outside that restriction.

## 4. Define the security boundary before building further

### Protected MVP boundary

- One gateway process handles agent requests.
- One artifact/publication backend runs as a separate service.
- Two deterministic agent client processes receive their own run credentials.
- A trusted controller provisions runs and controls demo scenarios.
- Only the gateway can reach the protected backend with valid service credentials.
- Agents cannot access the gateway database, backend database, service credentials, controller credentials, or administrative interface.
- Initial prompts, files, restored context, and tool results are labeled by trusted infrastructure.

Use the existing FastAPI, SQLite, and Vite stack where practical. SQLite is acceptable for a deliberately single-worker gateway with explicit transactions and serialization. A PostgreSQL or Redis migration is not a prerequisite for this demo.

```text
Trusted controller -- admin credentials --> private control interface
       |
       +-- run credential --> Agent A --+
       +-- run credential --> Agent B --+--> FlowGate --> Artifact service
                                             |              |
                                      policy/provenance   dispatch log,
                                      and run state      versions, outbox,
                                             |           operation receipts
                                             +------ dashboard/evidence export
```

### Trust assumptions

The gateway, controller, label store, backend, and their administrators are trusted. Agents, model output, tool arguments, and document text are untrusted. The host is not assumed to be compromised.

Run credentials are bearer capabilities in the minimal design. Do not claim they resist theft or deliberate sharing. Keep them separate between workers; use stronger process-bound credentials only if that threat is part of the demonstrated scope.

Network configuration and service authentication must make the gateway mandatory. CORS, an SDK wrapper, or instructions telling agents to use the gateway do not establish that boundary.

## 5. Adopt a precise confidentiality model

### 5.1 Keep access rights separate from information labels

Use two separate checks:

1. **Access authorization:** may this principal perform this operation on this resource?
2. **Information-flow authorization:** may the context already seen by this run flow to this destination?

Passing the second check must not grant write access to someone else's resource. A shared storage location does not make its contents public.

For a small demo, represent a confidentiality label as:

```text
label = (classification, compartments)

public:       (0, {})
alpha secret: (2, {alpha})
beta secret:  (2, {beta})
```

`classification` orders public < internal < secret. Compartments specify which project restrictions must be preserved. Ownership and ACLs are separate fields.

Each run has trusted access grants and a maximum clearance, plus an **effective context label**. Clearance describes what the run may read. Its effective label describes restrictions on the information it has actually received.

### 5.2 Accumulate restrictions on every input

```text
join((level_a, compartments_a), (level_b, compartments_b)) =
    (max(level_a, level_b), union(compartments_a, compartments_b))

effective_context = join(previous_context, incoming_data_label)
```

- Initialize the effective label from the run's prompt, files, and restored memory.
- On an authorized read, persist the increased label and the exact source version before releasing content to the agent.
- Never lower the label during the lifetime of the same retained agent context.
- Token rotation, reconnection, restart with restored memory, or a new run ID must not silently cleanse that context.
- A genuinely new clean session must not receive old confidential messages, summaries, files, or memory.

If the controller cannot reliably label an input, deny importing it into a clean context or apply a documented conservative restricted label. Do not treat agent-supplied labels as authoritative.

### 5.3 Propagate restrictions into internal artifacts

For creation of a derived internal artifact, the server assigns a label at least as restrictive as the run's effective context. The agent cannot label it public to evade the rule.

Store immutable versions and the conservative dependency edges from inputs to the creating run and output version. Preserve older provenance after updates.

An authorized collaborator may read a confidential shared draft. Its effective context then inherits the draft's restrictions, even if it never read the original source.

### 5.4 Check fixed destinations before execution

Permit a flow from source/context label `S` to destination label `D` only if:

```text
S.classification <= D.classification
and S.compartments is a subset of D.compartments
```

Also require ordinary access authorization. Destination labels are set by trusted configuration.

For existing resources, reject writes that do not fit the resource's fixed label. For a new internal artifact, the server can assign a suitably restrictive label. A public outbox has the fixed label `(0, {})`; never silently make it confidential to make publication appear allowed.

Apply these checks to all output-bearing operations, including tool arguments, names, metadata, and publication requests, not just a field called `content`. Keep destinations and resource IDs constrained to avoid leaking through attacker-selected names or URLs.

### 5.5 Accept and explain the conservative trade-off

After a run reads secret data, even an unrelated public note is blocked. That is an intentional restriction of the model, not evidence that the note contains a secret. Public work should use a genuinely clean session.

Declassification should be disabled for the first milestone. If later added, it must be an explicit trusted action tied to one immutable output version/hash, destination, approver, and expiry. The agent cannot approve its own declassification.

Content hashes, canaries, and overlap scores remain useful supporting evidence. Correct their public-content false positives, but do not make flow authorization depend on them finding the secret.

## 6. Improvements in priority order

### P0: Required for a credible security demonstration

| ID | Improvement | Main area | Acceptance evidence |
|---|---|---|---|
| P0-01 | Controller-issued identities and fixed grants; no agent self-assigned privileges | Auth, models, run creation | Agent cannot create privileged runs, mutate labels, or mint a replacement privileged identity. |
| P0-02 | Separate and authenticate admin/demo endpoints; protect event/resource views | API and dashboard auth | Agent credentials cannot reactivate runs, reset data, read unrelated audit records, or start administrative demos. |
| P0-03 | Effective context labels and destination flow checks | Policy, store, gateway | Excerpts, paraphrases, padding, and encodings from tracked restricted context cannot reach public sinks. |
| P0-04 | Versioned derived artifacts with inherited labels and retained provenance | Store and artifact service | A -> confidential draft version -> B -> attempted public destination is inspectable. |
| P0-05 | Correct legitimate public sharing | Policy and detector | Clean public read/copy/publish succeeds without suspension, including copying identical public text. |
| P0-06 | Mandatory backend mediation | Service credentials and network setup | Direct backend attempts from the agent environment fail before protected operation execution. |
| P0-07 | Independent execution evidence | Gateway events and backend receipts | Every denied publication has no matching dispatch or committed outbox object; allowed control has both. |
| P0-08 | Durable ordering, suspension, failure behavior, and retry safety | Transactions and request state | Race/failure tests preserve confidentiality; unknown outcomes are reconciled without duplicate publication. |
| P0-09 | Deterministic external two-run scenario and isolated baseline comparison | Runner and tests | Reproducible public success and restricted failure; comparison explains the contribution of flow state. |
| P0-10 | One-command or short documented setup and explicit scope | Packaging and README | Another person can start the demo and inspect evidence without editing application source. |

### P1: Improve the presentation after P0 works

- Show the full cross-run dependency path with resource versions, effective labels, blocked operation, policy reason, and backend evidence in one view.
- Let the viewer switch between public collaboration and the blocked confidential workflow. Keep unsafe baseline mode confined to synthetic test fixtures.
- Export a redacted JSON evidence bundle and a short Markdown scenario report.
- Add one real agent-framework or MCP adapter if the core loop is complete. The adapter must call the same enforcement path.
- If a model is used, let it create a draft or propose an action; do not let it set security labels or authorize publication. Keep a deterministic fallback.
- Add controller-side worker cancellation only if the demo claims that the process stops; otherwise say "gateway access revoked."
- Keep controls keyboard accessible, errors explicit, and decisions understandable without relying only on red/green colors.

### P2: Defer until after the hackathon core

Semantic leak classifiers, embeddings, multiple backend connectors, Slack alerts, a policy-builder UI, broad tenant onboarding, multi-worker deployment, database migration, production HA, and complex reviewed declassification.

Do not spend the remaining build time chasing universal covert-channel prevention or adding guardrail products without a measured benefit to the defined scenario.

## 7. Request ordering, retries, and evidence requirements

### Local gateway state

- Use explicit transaction boundaries for label/provenance changes and their associated audit records.
- For a read, commit effective-label increase and source-version provenance before delivering content. If persistence fails, do not return the confidential content.
- Serialize operations for each agent context. For the MVP, one gateway worker and a documented transaction/lock discipline are acceptable. A process-local lock does not provide multi-worker coordination.
- Define ordering at the read's durable label update and the write's serialized final authorization/dispatch boundary. An already-dispatched write with a bound immutable payload may finish after a later secret read. If the read takes effect first, subsequent public dispatch must be denied. Revalidate any previously authorized work that has not crossed the dispatch boundary, including recovered work.
- Check run status and current grants immediately before protected dispatch. Requests admitted after suspension commits must be denied.
- Suspension does not retroactively undo an operation already dispatched. Record in-flight outcomes honestly.
- Fail closed if trusted run state, labels, policies, or mandatory audit-intent storage are unavailable.
- Validate policy configuration at startup. State the rule precedence explicitly; the current code uses first-match rules. Mandatory confidentiality checks must not be bypassed by an early allow rule.

### External backend operations

An external backend cannot share an ordinary transaction with the gateway's SQLite database. Use a small durable state machine:

```text
RECEIVED -> DENIED
RECEIVED -> AUTHORIZED_INTENT -> DISPATCHED -> COMMITTED
                                           -> FAILED
                                           -> UNKNOWN -> reconciled outcome
```

Persist authorized intent before dispatch. Bind it to the run/context, operation, normalized destination, payload hash, policy version, and a stable request/idempotency key. The backend must authenticate the gateway and deduplicate execution by that key; reuse with a different payload or destination must fail.

After a timeout or crash, reconcile using the backend's receipt lookup before retrying with the same key. Never report "blocked before execution" if dispatch may already have occurred. `UNKNOWN` is a real outcome, not an automatic denial or success.

Keep three counts distinct: **transport dispatch attempts** by the gateway, **logical backend executions** after authentication/idempotency checks, and **committed objects**. Retries may produce multiple transport attempts but must not execute the logical publication more than once. A pre-dispatch denial requires zero protected-operation transport attempts and zero logical executions for that request. Log inbound rejected or deduplicated requests separately from executions.

### Minimum evidence fields

- Scenario ID, request ID, run/context ID, trusted principal, and timestamps.
- Operation and canonical resource/destination ID.
- Source resource IDs and immutable versions; effective label before and after the operation.
- Destination label and ACL decision.
- Policy version, decision, and human-readable reason.
- Structured matched fingerprint ID when the optional detector contributes evidence.
- Dispatch state, backend request ID, and backend receipt/outbox object ID when present.
- Run suspension event and, separately, controller stop confirmation if implemented.

Protect raw content and credentials in evidence exports. Shingles can expose source text and must be treated as sensitive derived data. Restrict detector/fingerprint access and avoid exposing other projects' secret content in reasons.

For a denial, independently check backend dispatch records and destination state. A red dashboard row or empty destination alone is insufficient: the backend could have been called and failed. For an allowed control, require a committed object and matching receipt to prove the service actually works.

## 8. The flagship demonstration

Use synthetic data. The main actors A and B are authorized collaborators for project Alpha, both allowed to read Alpha-secret artifacts. A separate Beta principal is used only for unauthorized-access controls.

| Fixture | Label | Purpose |
|---|---|---|
| `alpha-source:v1` | Secret, Alpha compartment | Original confidential document |
| `alpha-draft:v1` | Secret, Alpha compartment, assigned by server | Derived internal artifact |
| `public-source:v1` | Public, no compartments | Successful baseline |
| `public-draft:v1` | Public, no compartments | Authorized public handoff |
| `public-outbox` | Fixed public sink | Simulated external publication, no real email or internet post |

### Scenario A: Approved public collaboration

1. Controller creates two genuinely clean sessions with public access.
2. A reads the public source and creates a public draft.
3. B reads the draft and publishes to the local public outbox.
4. Show the backend dispatch receipt, committed object, and dependency path.

### Scenario B: Restricted collaboration, denied publication

1. Controller creates two fresh Alpha-authorized sessions.
2. A reads `alpha-source:v1`; its effective label becomes Alpha-secret.
3. A creates a short rewritten draft with no canary and little textual overlap. The server labels the artifact Alpha-secret.
4. B reads that draft successfully; its effective label becomes Alpha-secret.
5. B attempts to publish publicly. The gateway denies before publication dispatch because the destination cannot accept that context.
6. Show the source/draft/run path, policy decision, absence of a backend publication dispatch, and unchanged outbox.
7. If the explicit publication rule also suspends B, prove a later protected request is denied. Do not describe that as worker termination without controller evidence.

The draft can be generated deterministically; do not depend on a model choosing to attack during a live presentation.

### Scenario C: Explain why flow state matters

Replay the same synthetic workflow in isolated test configurations: static per-resource access checks only, then access checks plus inherited flow restrictions. With only static access checks, permitted reads and public publication can all succeed. With inherited flow restrictions, internal collaboration still succeeds but public publication is denied.

Do not expose a bypass switch in the normal agent interface. A standalone scanner may catch some payloads; this comparison does not claim all other security products fail.

### Three-minute presentation outline

- First 30 seconds: user, shared-artifact workflow, and the missing confidentiality boundary.
- Next 40 seconds: working public collaboration and a real backend receipt.
- Next 60 seconds: restricted handoff and blocked publication without a canary.
- Next 30 seconds: source versions, inherited labels, denial reason, and backend proof.
- Final 20 seconds: enforced scope, conservative trade-off, and the next integration.

## 9. Acceptance-test plan

**Every case below is PLANNED, not an observed pass.** When implemented, track status as `planned`, `implemented`, or `verified`, with command, commit/snapshot, runtime, date, and evidence location. Keep these cases separate from the previously observed 48-test suite.

Unless specified otherwise, a denied request must create no protected mutation and no authorized backend operation dispatch. Test through the actual HTTP boundary where relevant, then inspect backend evidence separately.

### A. Identity, administration, and access

| ID | Priority | Case | Required result |
|---|---|---|---|
| ID-01 | P0 | Missing, malformed, expired, or revoked run credential | Authentication fails; no protected dispatch. |
| ID-02 | P0 | Agent asks to create a secret-clearance run or change its grants | Denied; only the controller can provision trusted privileges. |
| ID-03 | P0 | Agent submits forged project, clearance, effective-label, or source-version fields | Trusted server state wins; no privilege or label reduction. |
| ID-04 | P0 | Agent calls reactivate, reset, demo-run, or privileged run-management endpoints | Denied; suspension and audit records remain unchanged. |
| ID-05 | P0 | Beta principal reads an Alpha artifact; low-clearance Alpha principal reads secret data | Denied before content delivery; no confidential response. |
| ID-06 | P0 | Principal has compatible confidentiality but lacks write permission for another owner's artifact | Denied by ACL; labels do not grant ownership rights. |
| ID-07 | P0 | Rotate credential, reconnect, or restore the same retained context under another run ID | Effective restrictions survive; no clean-context laundering. |
| ID-08 | P0 | Ordinary agent reads global graph, fingerprints, event history, or other projects' metadata | Denied or scoped to authorized records; no secret text, shingles, or credentials exposed. |

### B. Information-flow behavior

| ID | Priority | Case | Required result |
|---|---|---|---|
| FLOW-01 | P0 | Clean run copies identical public text between public resources | Allowed; no suspension; content and receipt match. |
| FLOW-02 | P0 | Full public A -> draft -> B -> outbox workflow | Allowed end to end; a committed backend publication exists. |
| FLOW-03 | P0 | Restricted A -> rewritten draft -> B -> public outbox workflow | Internal steps allowed; publication denied; inherited labels and zero publication dispatch verified. |
| FLOW-04 | P0 | Run starts with a confidential prompt, file, or restored memory and performs no tool read | Effective label is already restricted; public publication denied. |
| FLOW-05 | P0 | After a secret read, submit an exact copy, short excerpt, paraphrase, translation, encoding, or padded copy | All public writes denied by context flow policy, regardless of detector score. Parameterize the payloads. |
| FLOW-06 | P0 | After a secret read, split a leak over multiple short writes | Every incompatible public write denied; no fragment reaches the sink. |
| FLOW-07 | P0 | After a secret read, submit a harmless public greeting | Denied under the documented conservative policy; reason identifies context restrictions, not proven secret content. |
| FLOW-08 | P0 | High-clearance clean run that has received only public context publishes publicly | Allowed; maximum clearance alone must not taint the run. |
| FLOW-09 | P0 | Restricted run creates an artifact while requesting a public label | Server refuses downgrade or creates a restricted internal version; no public exposure. |
| FLOW-10 | P0 | A restricted run writes to a compatible, authorized internal destination | Allowed; resulting version retains restrictions and provenance. |
| FLOW-11 | P0 | Attempt same-project secret -> internal/public downgrade, or Alpha -> Beta transfer | Denied by classification or compartment rules. |
| FLOW-12 | P0 | Label a sensitive resource's storage location `shared` | Access and confidentiality restrictions still apply; `shared` is not a public exemption. |
| FLOW-13 | P0 | Confidential data is placed in a resource name, tool argument, metadata field, or publication title | Output operation is checked as a whole; no incompatible dispatch. |
| FLOW-14 | P0 | Document text instructs the agent to ignore restrictions or relabel its output | Content cannot modify trusted labels, grants, or policy. The test does not require detecting the injection text. |
| FLOW-15 | P1 | A genuinely new public-only session is started after a restricted run | Public publication succeeds only when the new session receives no old restricted context. |

### C. Provenance and backend evidence

| ID | Priority | Case | Required result |
|---|---|---|---|
| EVID-01 | P0 | Deny a publication with a unique request ID | Gateway denial exists; protected-operation transport attempts, logical backend executions, and committed objects for that ID are all zero. |
| EVID-02 | P0 | Allow a public publication with a unique request ID | Exactly one logical execution and committed object, with receipt matching payload hash and destination; any transport retries are counted separately. |
| EVID-03 | P0 | Inspect restricted two-run workflow | Evidence links source version -> A -> draft version -> B -> attempted destination and applicable policy version. |
| EVID-04 | P0 | Read v1, update the artifact to v2, then inspect history | v1 source content hash, label, and dependency links remain intact. |
| EVID-05 | P0 | Agent tries to delete events or alter labels/versions directly | Access denied; trusted records remain intact. Demo reset cannot remove security history in protected mode. |
| EVID-06 | P1 | Export the evidence bundle | No bearer/admin/service secrets or unauthorized source text; identifiers and outcomes are sufficient to verify the scenario. |
| EVID-07 | P1 | View dependency graph | Attempted/denied edges are distinct from completed transfers; conservative dependency is not presented as exact textual causality. |

### D. Concurrency, crashes, and retries

| ID | Priority | Case | Required result |
|---|---|---|---|
| REL-01 | P0 | Race secret read and public write on the same context | If the read's label update takes effect first, later public dispatch is denied. An immutable write dispatched first may finish later. Undispatched work is revalidated; response completion order is not the security ordering. |
| REL-02 | P0 | Fail the label/provenance transaction during a confidential read | No confidential content is returned and no partial clean-context success is reported. |
| REL-03 | P0 | Restart gateway after a secret read | Durable restrictions and suspension survive; initial seed logic does not erase protected state. |
| REL-04 | P0 | Suspend while calls are queued | Requests admitted after suspension commits are denied; already-dispatched work is tracked separately. |
| REL-05 | P0 | Backend unavailable, or response is lost after possible commit | Outcome is failed or unknown as appropriate; never falsely reported as a pre-execution block. |
| REL-06 | P0 | Retry an allowed publication after backend commit and lost response | Reconcile/retry with the same key; exactly one committed publication. |
| REL-07 | P0 | Reuse a request/idempotency key with a changed payload, destination, or principal | Rejected; no replay of the old authorization against new data. |
| REL-08 | P0 | Crash after durable authorization intent but before dispatch | On recovery, revalidate current restrictions before any new dispatch; retain a consistent request history. |
| REL-09 | P0 | Invalid policy file, missing trusted labels, or unavailable mandatory audit-intent storage | Startup fails or request is denied before dispatch; no default allow. |
| REL-10 | P0 | Two calls update the same artifact concurrently | Expected-version conflict or defined serialization; no lost version or missing provenance. |
| REL-11 | P0 | Oversized body, missing write content, unknown tool, wrong resource kind, or unsafe identifier | Bounded validation error; no silent empty overwrite or unintended resource access. |
| REL-12 | P1 | Mandatory optional scanner times out, if such a scanner is added | Hold/deny according to explicit policy; no bypass disguised as a clean result. |

### E. Deployment boundary and demo experience

| ID | Priority | Case | Required result |
|---|---|---|---|
| DEMO-01 | P0 | Agent environment calls backend directly, tries alternate URL/port, or lacks gateway service credential | No protected operation executes. Test from the actual deployed agent environment. |
| DEMO-02 | P0 | Run both flagship scenarios with two separate external client processes | Actual HTTP gateway path is exercised; no internal helper calls substitute for the demonstration. |
| DEMO-03 | P0 | Replay synthetic static-ACL baseline and full-flow mode | Same fixtures show the difference; normal agent API cannot select unsafe mode. |
| DEMO-04 | P0 | Follow startup instructions on a clean checkout/environment | Required services start; health checks and scenario commands work without source edits. |
| DEMO-05 | P1 | Repeat demo five times with isolated scenario IDs | Expected public/restricted outcomes every time; no leftover-state false positives or erased evidence. |
| DEMO-06 | P1 | Backend offline or authorization expires while viewing dashboard | Clear error and retry path; UI does not show stale data as current proof. |
| DEMO-07 | P1 | Complete demo controls using keyboard and inspect narrow viewport | Controls remain usable; decisions are labeled in text; graph is supplemented by an accessible event list. |
| DEMO-08 | P1 | Real model/framework adapter proposes publication, if implemented | Same enforcement and evidence as deterministic client; provider failure has a deterministic demo fallback. |
| DEMO-09 | P1 | Claim worker stopped, if implemented | Controller confirms process termination separately from gateway suspension. |

### Test harness rules

- Use synthetic fixtures and fresh temporary databases; do not run destructive resets against a developer's active database.
- Give each scenario and request a stable unique ID. Inspect backend receipts through a trusted test/controller channel unavailable to agents.
- Assert both denial and successful collaboration. A gateway that blocks everything must fail the suite.
- Parameterize adversarial payload variants; do not tune fixtures solely to make a detector score pass.
- Inject failures at transaction, dispatch, commit, and response boundaries.
- Use repeated concurrent runs for race cases, while also checking explicit state-transition invariants; repetition alone is not proof.
- Keep unit tests for normalization/hashes separate from security acceptance tests.
- Do not preserve an old expected result when it encodes a design flaw, such as suspending a clean public copy.

## 10. Delivery sequence and ownership

Complete milestones in order. Each milestone ends with recorded acceptance evidence rather than a fixed date estimate.

| Milestone | Deliverable | Primary owner | Exit condition |
|---|---|---|---|
| M0 | Threat boundary and versioned contract amendment | Human lead + backend-core; verifier reviews assumptions | Label semantics, access checks, endpoint roles, ordering, and evidence fields agree with this scope. |
| M1 | Trusted identities, protected admin API, scoped views | Backend-core | Identity/admin acceptance cases pass; agent cannot grant itself authority. |
| M2 | Durable effective context and inherited artifact versions | Backend-core + tools/seed | Public copy succeeds; restricted same-run and cross-run publication is denied without content matching. |
| M3 | Separate protected backend and verifiable publication receipts | Tools/seed + backend-core | Direct bypass fails; allowed control commits; denied control has zero publication dispatch. |
| M4 | Failure/retry ordering and external scenario suite | Demo/tests + integration | P0 concurrency, restart, idempotency, and crash cases pass. |
| M5 | Evidence-first dashboard and reproducible demo | Frontend + integration | Another person can run both workflows and inspect the full evidence chain. |
| M6 | Optional real-agent/MCP adapter and presentation rehearsal | Assigned integration owner | Adapter uses the same policy path; deterministic fallback remains available. |

If time becomes tight, cut M6 and optional UI features. Keep trusted identity, public success, restricted handoff, mandatory backend mediation, and independent evidence. If those are not complete, present a limited prototype and say which guarantees remain unverified.

### Existing repository areas to update when implementation is authorized

- `backend/app/auth.py`, `models.py`, `main.py`: trusted principal/context handling, protected endpoint roles, input validation, and safe orchestration.
- `backend/app/policy.py`, `backend/policies.yaml`: access rules plus mandatory flow checks, with explicit precedence and configuration validation.
- `backend/app/store.py`: durable context state, immutable versions, structured provenance, request lifecycle, and coherent transactions.
- `backend/app/detection.py`: public-content correctness and supporting evidence; retain pure functions where practical.
- `backend/app/tools.py`, `backend/seed.py`: protected adapter, publication operation, and synthetic labeled fixtures; move independent backend behavior into a small dedicated service when building M3.
- `scripts/demo.py`, `backend/tests/`: external clients, positive/negative scenarios, fault injection, and receipt assertions.
- `frontend/src/`: authenticated evidence views, version-aware dependency graph, and clear completed-versus-attempted actions.
- `README.md`, `CONTRACT.md`, `MVP.txt`: one current story, a versioned specification, and clearly marked historical proposals.

These are proposed changes only. No repository file was changed to produce this roadmap.

## 11. Improve the `.claude` agent workflow

Keep the seven existing roles. Improve their authority boundaries and handoffs before adding roles.

1. **Permit findings against the contract.** The adversarial verifier must report a security hole even when implementation matches `CONTRACT.md`. Contract conformance and security validity are separate assessments.
2. **Make amendments reviewable.** Builders do not casually change the spec. A designated contract owner records the reason, new invariant, API/schema change, and regression case before integration adopts an amendment.
3. **Make verification mandatory.** Use builders -> integration -> adversarial review -> fixes -> verifier rerun. Replace optional "if given verifier findings" handling with a release gate.
4. **Update file ownership for new work.** Explicitly assign the artifact backend, context-label logic, fault tests, and packaging files before parallel edits. Keep a single owner for each shared schema.
5. **Require useful handoffs.** Include files changed, spec version, exact commands/results, acceptance IDs covered, remaining risks, and where backend evidence is stored.
6. **Test boundaries, not just scores.** Detector tests validate helper behavior; demo and adversarial tests validate unauthorized operations never executing and legitimate operations succeeding.
7. **Ensure all tests have an executing owner.** The current detection role owns policy tests while only running detection tests. Integration must explicitly run the complete suite after dependencies land.
8. **Preserve verifier independence.** The verifier reports and reproduces findings; an assigned builder fixes them; the verifier reruns the reproduction.

## 12. Reproducibility and documentation improvements

- Record one tested Python version and one Node version; pin or lock dependencies sufficiently for another machine to reproduce the demo.
- Separate runtime and development dependencies where useful; keep secrets out of source and provide only non-secret configuration examples.
- Use version control and ignore local databases, caches, build output, environment files, and credentials. Preserve synthetic fixtures in source.
- Remove import-time and normal-startup destructive seeding; initialize demo data explicitly in an isolated scenario environment.
- Provide health checks and a short startup/scenario/evidence-export sequence. If using Compose, document the agent-to-backend network restriction and test it.
- Mark `MVP.txt` as historical or reconcile it with the chosen implementation. The current proposal and contract disagree about several stack choices and policy precedence.
- State which behavior is implemented, simulated, verified, or planned. Keep test counts generated from results rather than treating a README number as a release gate.
- Replace the broad tagline "We isolate information" with the narrower pitch in section 2 until the stronger boundary is demonstrated.
- Describe the graph as conservative dependencies and attempts, with completed transfers identified separately.

### Existing checks that can be run now

From the repository root, in PowerShell:

```powershell
# Use an installed Python environment with the backend dependencies.
# The in-memory override avoids import-time access to the normal gateway.db.
$previousGatewayDb = $env:GATEWAY_DB
try {
    $env:GATEWAY_DB = ':memory:'
    Push-Location backend
    try { python -m pytest tests/ -q } finally { Pop-Location }
} finally {
    $env:GATEWAY_DB = $previousGatewayDb
}

Push-Location frontend
npm run build
Pop-Location
```

These run existing tests and the build only. They do not implement or verify the planned acceptance matrix. The earlier successful backend review used Python 3.13; an environment-specific sandbox loopback restriction had to be resolved before TestClient could run.

When the new harness exists, document its actual command and evidence-output path. Do not publish placeholder commands as if they already work.

## 13. Hackathon readiness gates

The proposed product is ready to present as a working, scoped security demonstration when:

- All P0 acceptance cases are verified on the exact demo snapshot with no unresolved confidentiality or administrative bypass in scope.
- Public collaboration produces a real committed backend object.
- Restricted collaboration reaches the internal draft and second run successfully, but public publication has zero backend publication dispatch and zero committed object.
- No canary, keyword, exact match, or model judgment is necessary for the principal confidentiality decision.
- Direct backend access from the agent environment is tested and blocked.
- Provenance survives updates and restarts, and evidence can be inspected independently of the dashboard.
- Failure and unknown-outcome behavior are honest and retry-safe.
- A teammate can reproduce the demonstration from the instructions.
- The pitch accurately states the protected boundary, trust assumptions, and conservative restriction on public output after secret input.

Useful measurements include scenario pass counts, prohibited dispatch counts, successful public operations, local policy-check latency (p50/p95), time to start the demo, and time to locate a denial's evidence. Record the machine, sample size, and whether scanner/model time is included. Set performance targets after measuring; do not invent achieved numbers.

Passing a finite test suite supports the defined demonstration. It is not a proof of production security or universal noninterference.

## 14. Positioning and next validation

Existing tools already offer substantial gateway and runtime-security functionality. The proposed focus on persistent restrictions across separate runs and artifact versions is a differentiation hypothesis to test, not a verified claim that nobody else does it.

Relevant official references examined during the assessment:

- [Invariant Guardrails](https://github.com/invariantlabs-ai/invariant): contextual policies across tool interactions and MCP/LLM interception.
- [Mintive, formerly MintMCP](https://www.mintmcp.com/): agent identities, scoped access, gateway governance, and monitoring/data protection.
- [Lasso MCP Gateway](https://github.com/lasso-security/mcp-gateway): MCP proxying and sensitive-information sanitization.
- [agentgateway MCP authorization](https://agentgateway.dev/docs/standalone/latest/documentation/mcp/mcp-authz/): authorization for tools, prompts, and resources.
- [Snyk Evo Agentic Development Security](https://snyk.io/evo/agentic-development-security/): broader agent-tool governance and runtime controls.
- [CaMeL research](https://arxiv.org/abs/2503.18813): capability and information-flow approaches to unauthorized data exfiltration.
- [Meta LlamaFirewall](https://github.com/meta-llama/PurpleLlama/tree/main/LlamaFirewall): related scanners and guardrails; an optional additional layer rather than the authority for run permissions or artifact labels.

The comparison is based on documentation, not a hands-on competitive benchmark. Availability and vendor features can change.

For a future product decision, speak with a few teams operating shared agent infrastructure. Ask for a real workflow, how current permissions/DLP handle it, where that fails, what integration they would accept, and what evidence would make them trust enforcement. Do not assume a technically interesting boundary is already a recurring purchasing need.

Verify current hackathon rules, required technologies, submission format, and deadline with the organizer before finalizing optional integrations. This roadmap does not assume that a particular vendor integration is mandatory or that technical readiness guarantees judging success.

## 15. Immediate next action

When implementation is authorized, begin with M0 and M1: agree on the confidentiality and endpoint-role contract, then close self-provisioned identity and administrative bypasses. Implement the public-success and restricted-handoff acceptance cases early. Let those outcomes guide the remaining work.

**The finish line is a permitted collaboration that works and a prohibited publication that demonstrably never executes through the enforced boundary.**
