# INTERFACE_CONTRACT_MODEL.md

# Cortex Interface Contract Model

Version: 1.1

Status: Canonical

Authority: Highest

---

# Revision Note (1.1)

A focused canonical review against `WORKFLOW_MODEL.md`'s Authority clause
("Every execution performed by Cortex must occur through a registered
Workflow. No execution path may bypass the Workflow Model.") found that
Section 5 and Section 6 of version 1.0 described Core → Service
invocation without naming the Workflow that must mediate it, and that no
Command, example, or Forbidden-edge entry addressed retry, resume,
cancellation, or analytics synchronization. These are documentation-only
corrections: no new architectural rule was introduced, because the
corrected wording is a direct, unambiguous consequence of rules already
explicit in `WORKFLOW_MODEL.md` (Authority, Workflow Registry, Workflow
Types, Failure Handling) and `CORTEX_ARCHITECTURE.md` (Rule 3,
Dependency Direction). See the affected sections below for the specific
changes.

---

# 1. Purpose and Scope

This document defines how every Interface communicates with Cortex.

An Interface is any surface through which a human or an external system
reaches the platform. Examples include the Dashboard, the CLI, the REST
API, the SDK, Webhooks, and any future Interface.

This document does not describe implementation. It does not describe UI
design. It does not describe roadmap. It describes the permanent contract
boundary between every Interface and the rest of Cortex.

It exists because `CORTEX_ARCHITECTURE.md` establishes that Interfaces
never own business logic, but does not itself specify the concrete shape
of Interface communication. This document is that specification. Every
Dashboard, REST API, CLI, SDK, Webhook, and future Interface must conform
to it.

Reviewed for conflicts against `PROJECT_NORTH_STAR.md`,
`FACTORY_PHILOSOPHY.md`, `CORTEX_ARCHITECTURE.md`, `DESIGN_PRINCIPLES.md`,
`EXECUTION_MODEL.md`, `SERVICE_MODEL.md`, `WORKFLOW_MODEL.md`,
`SKILLS_SPEC.md`, and `GOVERNANCE.md`. No conflicts were found. This
document makes explicit several rules that those documents imply but do
not state directly (most notably: Interfaces communicate with the Core
only, never with Services, Infrastructure, or Channel Packages directly;
and the ownership split between Authentication and Authorization). Where
this document is silent, the higher-authority documents govern per
`GOVERNANCE.md`'s Authority Order.

---

# Core Principle

An Interface observes, requests, and displays.

The Core decides, executes, and owns state.

An Interface that makes a decision the Core should have made is an
architectural defect, regardless of which Interface it is.

---

# 2. Interface Responsibilities

Every Interface is responsible for, and only for:

- Presenting information to a human or external system.
- Accepting input and translating it into a Command or Query.
- Submitting Commands and Queries to the Core.
- Rendering the Core's responses, including progress and error state.
- Authenticating the caller (see Section 10).
- Enforcing its own presentation-layer concerns: layout, formatting,
  pagination, localization, input ergonomics.

An Interface's responsibility ends at the boundary of the request it
sends and the response it renders. It owns none of what happens in
between.

---

# 3. What Interfaces Are Explicitly Forbidden To Do

An Interface must never:

- Implement or duplicate a business rule (eligibility, approval,
  scoring, scheduling, retry policy, or any other decision defined by a
  Service or the Core).
- Decide whether an operation is authorized. It may confirm the caller
  is authenticated; the Core decides what an authenticated caller may
  do.
- Call a Service directly. Every request passes through the Core (see
  Section 4).
- Call Infrastructure directly for anything other than its own
  authentication needs (Section 10). Storage, queues, providers, and
  databases are not Interface concerns.
- Read from or write to Channel Package data directly. Channel identity,
  policy, and objectives are loaded by the Core into the Execution
  Context; the Interface only ever sees what the Core returns.
- Maintain its own copy of execution state as a source of truth. An
  Interface may cache what it has been told for rendering performance,
  but the Core's state is always authoritative and the cache is always
  disposable.
- Invent its own error categories, retry semantics, or state machine.
  It renders the categories and states the Core defines (Sections 11,
  13).
- Assume it is the only Interface acting on the platform. Two
  Interfaces must never produce different outcomes for the identical
  Command (Section 17).

This section is the concrete elaboration of `CORTEX_ARCHITECTURE.md`
Rule 3 ("Interfaces never own business logic") and of the explicitly
forbidden dependency "Dashboard → Business Logic."

---

# 4. Interface → Core Interaction Model

An Interface communicates with Cortex through exactly one door: the
Core. There is no other entry point.

```
Interface

  ↓  Command or Query

Core

  ↓  Response, Execution Reference, or Query Result

Interface
```

This is a direct application of `EXECUTION_MODEL.md`'s Step 1: every
execution begins when "Cortex receives a valid request," and the
document's own examples of such a request — scheduled production,
manual execution, API request, retry request, background worker — are
exactly the set of things every Interface, present or future, submits.

An Interface never skips the Core to reach a Service, Infrastructure, or
a Channel Package, even transitively. This is a clarification of
`CORTEX_ARCHITECTURE.md`'s Dependency Direction: the layered diagram
shows Interfaces depending on Core, Core on Services, Services on
Infrastructure — it does not show, and does not permit, an arrow from
Interfaces to any layer below Core. `SERVICE_MODEL.md`'s Communication
section establishes that Service-to-Service calls are only ever
mediated by the Core; the identical rule applies one layer up, to
Interface-to-Service calls.

---

# 5. Core → Service Interaction Model

Once the Core accepts a Command from an Interface, the Core never
invokes a Service directly. The Core resolves the Command to exactly one
registered Workflow (Section 6) and executes that Workflow; the Workflow
— not the Core acting alone — invokes Services through its Stages. This
is a direct application of `WORKFLOW_MODEL.md`'s own Authority: "Every
execution performed by Cortex must occur through a registered Workflow.
No execution path may bypass the Workflow Model." Everything past this
point is internal to Cortex and invisible to the Interface except through
the contracts defined in this document (Execution References, Query
results, Events).

```
Core

  ↓  resolves Command to a registered Workflow

Workflow

  ↓

Stage  →  Service A  →  Core  →  Stage  →  Service B
```

Per `WORKFLOW_MODEL.md`'s own composition (`Workflow → Stage → Service →
Agent → Task`), a Stage invokes the Service(s) it requires; per
`SERVICE_MODEL.md`, all Service-to-Service communication is still
coordinated by the Core — Services never call each other directly, and a
Service never owns a Channel Package or another Service's contract. The
Interface has no visibility into which Workflow, Stages, or Services
were invoked, in what order, or how many — it sees only the Workflow's
externally observable state (Section 12).

---

# 6. Command Contracts

A Command is a request to change platform state or trigger an
execution. It corresponds to `EXECUTION_MODEL.md`'s "Execution Request."

Every Command has the same envelope shape, regardless of which Interface
issues it:

```
command_type       — What is being requested (e.g. "ProduceEpisode",
                      "PublishContent", "SynchronizeAnalytics",
                      "ApproveCandidate", "RetryExecution",
                      "ResumeExecution", "CancelExecution")
idempotency_key     — Caller-supplied or deterministically derived;
                      see Section 14
payload             — The command-specific input
requested_by        — The authenticated caller identity (Section 10)
requested_at        — Timestamp
channel_id          — Which Channel Package this Command targets, if
                      applicable
```

Every `command_type` must resolve, inside the Core, to exactly one
registered Workflow (`WORKFLOW_MODEL.md`'s Workflow Registry: Name,
Version, Stages, Dependencies, Required Services, Entry Conditions, Exit
Conditions). A Command whose `command_type` has no matching registered
Workflow is rejected (Section 13, `Validation` category) — the Core
never falls back to executing ad hoc business logic in place of a
Workflow lookup, however small the requested change appears. This holds
even for Commands whose Workflow happens to have a single Stage (e.g.
`ApproveCandidate`): a trivial Workflow is still a registered Workflow,
not an exception to registration.

`command_type` maps onto `WORKFLOW_MODEL.md`'s own named Workflow Types:
`ProduceEpisode` → Production Workflow, `PublishContent` → Publishing
Workflow, `SynchronizeAnalytics` → Synchronization Workflow. `RetryExecution`,
`ResumeExecution`, and `CancelExecution` are Commands whose payload
identifies an existing `execution_id` (Section 11) rather than starting
new production input; the Core resolves them to that execution's
already-running Workflow and applies one of `WORKFLOW_MODEL.md`'s
Failure Handling actions — Retry Stage, Skip Stage, Substitute Service,
Abort Workflow — "Recovery policies are defined by the Core," never by
the Interface that issued the Command.

The Core validates the Command (Section 9), checks authorization
(Section 10), and either:

- Rejects it (with a structured error, Section 13), or
- Accepts it and returns an Execution Reference (Section 11), never the
  final result synchronously.

A Command never returns business data as its direct response. It
returns an acknowledgment that an execution has begun (or a rejection).
This is a direct consequence of `EXECUTION_MODEL.md`: nothing happens
until an execution begins, and the execution's own lifecycle — not the
Command call — is what produces results.

---

# 7. Query Contracts

A Query is a request to read state. It never changes anything and never
triggers a Workflow. It corresponds to no step in `EXECUTION_MODEL.md`'s
lifecycle because Queries do not create executions at all — they read
what the Core, Services, and Workflows have already published as
observable state (`SERVICE_MODEL.md` Observability, `WORKFLOW_MODEL.md`
Observability).

Every Query has the same envelope shape:

```
query_type          — What is being asked (e.g. "GetExecutionStatus",
                      "ListAnalytics", "GetProductionQueue")
parameters          — Query-specific filters (execution_id, channel_id,
                      date range, pagination)
requested_by        — The authenticated caller identity
```

A Query result is always a direct, current snapshot of state the Core
already owns. A Query must never have side effects. An Interface must
never infer or reconstruct state that the Core has not explicitly
returned.

---

# 8. Event Contracts

An Event is an asynchronous, one-directional notification from the Core
to a subscribed Interface that some observable state has changed. Events
exist so that Interfaces which need near-real-time updates (a live
Dashboard view, a webhook subscriber) do not have to poll Queries
continuously; they are an optimization over Queries, never a
replacement for the authority Queries provide.

Every Event has the same envelope shape:

```
event_type          — What changed (e.g. "ExecutionStateChanged",
                      "StageCompleted", "PublishingSucceeded")
execution_id        — Which execution this Event concerns
occurred_at         — Timestamp
state               — The new observable state (Section 12's vocabulary)
```

Rules:

- Events are derived from the same state Queries expose. An Interface
  must be able to reconstruct identical understanding by polling Queries
  alone; Events only change latency, never meaning.
- An Interface must never treat an Event as guaranteed-delivered. A
  missed Event must be recoverable by a subsequent Query. Webhooks in
  particular must be designed assuming at-least-once, possibly-duplicate
  delivery.
- Emitting an Event is the Core's responsibility. No Service or
  Interface emits an Event on the Core's behalf.
- An Event is an observation, never a trigger. An Interface may react to
  an Event only by re-rendering state or by presenting a human with the
  option to submit a new Command (e.g. a "Retry" button after a `Failed`
  Event). An Interface must never use an Event to decide, on its own,
  that a Command should be submitted automatically (auto-retry,
  auto-cancel, auto-resume) — doing so would reconstruct exactly the
  business logic (retry policy, recovery decisions) Section 3 already
  forbids Interfaces from owning, and that `WORKFLOW_MODEL.md`'s Failure
  Handling assigns to the Core alone.

---

# 9. Validation Boundaries

There are two distinct kinds of validation, and an Interface performs
only the first:

**Shape validation** (Interface-owned): is the request well-formed —
correct types, required fields present, syntactically valid. This is a
courtesy that fails fast and cheaply, consistent with
`DESIGN_PRINCIPLES.md` Principle 12 (Fail Fast). It never encodes a
business rule.

**Business and policy validation** (Core-owned): everything in
`EXECUTION_MODEL.md` Step 2 — platform configuration, runtime
compatibility, Channel Package, required services, secrets, policies,
available providers — plus every Service-level "validation error" and
"business rule violation" named in `SERVICE_MODEL.md`'s Error Handling
section. This validation happens once, in the Core and the Services it
coordinates, regardless of which Interface the request arrived through.

An Interface must never treat its own shape validation as a substitute
for the Core's business validation, and must never attempt to
second-guess, cache, or pre-empt a Core rejection. If shape validation
passes, the request is submitted; the Core's answer is authoritative.

---

# 10. Authentication vs Authorization Ownership

This is the one place where this document introduces a new ownership
split rather than restating an existing one, because none of the nine
reviewed documents name an Identity or Auth Service. The split below is
derived directly from existing layer responsibilities and introduces no
new layer:

**Authentication — who is calling — is an Interface + Infrastructure
concern.** Every Interface is responsible for establishing that the
caller is who they claim to be before it ever submits a Command or
Query, using credential/session/token verification that Infrastructure
provides (`CORTEX_ARCHITECTURE.md` Layer 4 lists Secrets as an
Infrastructure example). Each Interface may authenticate differently —
session cookies for the Dashboard, API keys for the REST API and SDK,
signed payloads for Webhooks, local credentials for the CLI — because
authentication mechanics are presentation-adjacent and Interface-
specific. What every mechanism must produce is the same thing: a
verified caller identity attached to `requested_by` on every Command and
Query.

**Authorization — what an authenticated caller may do — is a Core
concern.** `CORTEX_ARCHITECTURE.md` Layer 2 lists "Policy enforcement"
as a Core responsibility, not an Interface one. Every Command and Query
is authorized once, by the Core, using the same policy regardless of
which Interface it arrived through. An Interface must never filter,
gate, or hide functionality based on its own understanding of role or
permission beyond cosmetic convenience (e.g., hiding a button that would
only ever be rejected) — the Core's rejection is what actually enforces
the boundary, and the Interface's UI behavior must never be the only
enforcement.

This split directly addresses the structural conflict already recorded
in `docs/architecture/CORTEX_ALIGNMENT_REPORT.md` Major Conflict #4 (the
legacy Dashboard design docs describe the Dashboard's own backend
enforcing eligibility, model-swap approval, and RBAC filtering). This
document does not resolve that conflict in the running system — no
implementation or Dashboard code changes are made here — but it fixes
the correct rule going forward: RBAC and eligibility decisions belong to
Core policy enforcement, never to an Interface's backend, no matter how
close to the Interface that backend appears to sit architecturally.

Fail Closed (`DESIGN_PRINCIPLES.md` Principle 13) applies to both
halves: if the Interface cannot authenticate the caller, it must not
submit the request. If the Core cannot determine authorization, it must
reject rather than default to allow.

---

# 11. Long-Running Operations

Most Commands trigger a Workflow that outlives the request/response
cycle (`WORKFLOW_MODEL.md`; `EXECUTION_MODEL.md`'s full lifecycle from
Execution Request through Shutdown). An Interface must never block
synchronously waiting for a long-running operation to finish.

Instead, accepting a Command returns an Execution Reference immediately:

```
execution_id
initial_state       — always "Pending" at this point
```

The Interface uses this `execution_id` to check progress via Query
(Section 7) or Event subscription (Section 8). State values are exactly
`EXECUTION_MODEL.md`'s State Management vocabulary — Pending, Running,
Waiting, Retrying, Completed, Failed, Cancelled — reused verbatim so
that no Interface ever invents its own status vocabulary or translates
between two competing sets of state names.

---

# 12. Progress Reporting

Progress is exposed the same way regardless of which Interface asks for
it, using the observability fields `WORKFLOW_MODEL.md` and
`SERVICE_MODEL.md` already define:

```
execution_id
state               — current lifecycle state (Section 11)
active_stage        — current Workflow Stage, if any
completed_stages    — Stages finished so far
failed_stages       — Stages that failed, if any
started_at
updated_at
```

A Dashboard renders this as a progress bar; a CLI renders it as text; a
REST API returns it as JSON; an SDK wraps it in a typed object. The
underlying fields, and their meaning, are identical across all of them.
Nothing about progress reporting is Interface-specific except the
rendering.

---

# 13. Error Handling

Every error an Interface receives is pre-classified by the Core using
`EXECUTION_MODEL.md`'s existing Error Handling categories — Validation,
Provider, Runtime, Policy, Resource, Internal. An Interface translates a
category into its own idiom (an HTTP status code, a CLI exit code, a
Dashboard banner, an SDK exception type) but never re-classifies,
merges, or invents a new category.

```
error_category      — one of the six EXECUTION_MODEL categories
error_code          — stable, machine-readable identifier
message             — human-readable description
retryable           — whether the Core considers this safe to retry
                      (informational; see Section 14 — this does not
                      grant the Interface its own retry policy)
```

`retryable` tells the Interface whether re-submitting is meaningful, not
whether the Interface should implement its own retry loop or backoff
strategy — retry policy itself is a Core/Service concern
(`WORKFLOW_MODEL.md` Failure Handling), consistent with this session's
existing bounded auto-retry mechanism living in the queue/orchestration
layer, not in any Interface.

---

# 14. Idempotency

Because `EXECUTION_MODEL.md` names "Retry request" as a first-class
Execution Request alongside "Manual execution" and "API request," the
model already assumes Commands can be legitimately resubmitted — by a
human retrying a failed action, by an Interface retrying a dropped
connection, or by the platform's own retry mechanisms. Every Command
must therefore be safe to submit more than once.

Every Command carries an `idempotency_key`. The Interface either
supplies one explicitly (preferred for user-initiated actions, so a
double-click or a dropped connection cannot create two executions) or
the Core derives one deterministically from the Command's own identity
fields (e.g. `episode_id` + `command_type`) when the Interface does not
supply one. The Core deduplicates: a Command with a previously-seen
idempotency key returns the existing Execution Reference rather than
creating a new execution.

An Interface must never implement its own deduplication (e.g. disabling
a submit button as the sole protection) as a substitute for a real
idempotency key — that is a UX courtesy, not the enforcement mechanism.

---

# 15. Observability

An Interface is a consumer of observability, never its source. Every
execution, Workflow, and Service already exposes status, duration,
errors, resource usage, and provider usage (`SERVICE_MODEL.md`,
`WORKFLOW_MODEL.md` Observability sections). An Interface's own
observability responsibility is limited to: logging its own
request/response traffic (for debugging the Interface itself) and
surfacing what the Core reports. It must never become a second, parallel
source of truth for what actually happened during execution.

---

# 16. Replaceability

Every Interface must be replaceable without any change to the Core,
Services, Infrastructure, or Channel Packages — this is the direct
application of `CORTEX_ARCHITECTURE.md`'s Replaceability section
("Replace Dashboard without changing Core") and
`FACTORY_PHILOSOPHY.md` Principle 15 ("The Dashboard is replaceable.
Deleting the Dashboard should never stop Cortex."). Because every
Interface communicates exclusively through the Command/Query/Event
contracts defined in this document, replacing one — swapping the
Dashboard for a new frontend, adding a second REST API version, retiring
the CLI — requires no change anywhere below the Core. If replacing an
Interface ever requires touching a Service, a Channel Package, or the
Core's internals, that is proof the Interface was not honoring this
contract.

---

# 17. Multi-Interface Consistency

The identical Command submitted through the Dashboard, the CLI, the
REST API, or the SDK must produce the identical execution, the identical
authorization outcome, and the identical result. This is guaranteed
structurally, not by convention: because every Interface is required by
Section 4 to reach the Core through the same Command contract, and the
Core is the sole owner of business logic, validation, and authorization
(Sections 2, 9, 10), there is no code path by which two Interfaces could
diverge in outcome for the same request. Section 20's examples exist
specifically to demonstrate this in concrete form.

---

# 18. Versioning

Command, Query, and Event contracts are versioned artifacts, per
`DESIGN_PRINCIPLES.md` Principle 23 ("Version Everything") and
`GOVERNANCE.md`'s Major/Minor/Patch scheme:

- **Major** — a breaking change to a Command/Query/Event's shape or
  meaning (a required field removed, a state name changed). Requires an
  ADR per `GOVERNANCE.md`'s Breaking Changes rules and a migration path
  for every existing Interface.
- **Minor** — a new optional field, a new Command/Query/Event type added
  without changing existing ones.
- **Patch** — clarification with no behavioral change.

An Interface declares which contract version it speaks. The Core must
support at least the current and immediately prior major version
simultaneously during any migration window, consistent with
`DESIGN_PRINCIPLES.md` Principle 19 (Backward Compatibility).

---

# 19. Dependency Direction

This document does not alter `CORTEX_ARCHITECTURE.md`'s Dependency
Direction; it enforces it precisely:

```
Allowed

Interfaces → Core → Services → Infrastructure → Channel Packages

Forbidden

Channel Package → Core
Dashboard → Business Logic
Services → Dashboard
Infrastructure → Identity
Interfaces → Services            (new: layer-skipping)
Interfaces → Infrastructure       (new: layer-skipping, except
                                   authentication, Section 10)
Interfaces → Channel Packages     (new: layer-skipping)
Core → Service (bypassing a registered Workflow)   (new: see below)
```

The three interface-side "new" forbidden edges are not additions to the
architecture — they follow necessarily from the existing diagram, which
draws no arrow from Interfaces to any layer below Core. The fourth —
Core directly invoking a Service without a registered Workflow — is
likewise not an addition: it is `WORKFLOW_MODEL.md`'s own Authority
clause ("No execution path may bypass the Workflow Model") stated as a
dependency-direction edge, so that this document's Forbidden list is a
complete account of every way an Interface-originated request could
reach a Service incorrectly, not only the Interface-side half of it.
This document names all four explicitly because a contract specification
must remove any ambiguity about layer-skipping or Workflow-bypassing
that a purely structural diagram could leave open.

---

# 20. Examples

The examples below use the identical Command/Query/Event/Execution
Reference shapes defined in Sections 6, 7, 8, and 11. Only the transport
(HTTP form post, terminal invocation, JSON body, webhook delivery)
differs.

## Dashboard requesting production

```
Interface: Dashboard
Command:
  command_type:    "ProduceEpisode"
  idempotency_key:  "produce:hyp_ep_0042"
  payload:          { topic_id: "hyp_topic_0042", channel_id: "hypogeum" }
  requested_by:     "operator:muhamed"
Core response:
  execution_id:     "exec_9f21"
  initial_state:    "Pending"
Dashboard then:
  polls Query GetExecutionStatus(execution_id="exec_9f21")
  or subscribes to ExecutionStateChanged Events for the same id,
  and renders Section 12's progress fields as a progress view.
```

## Dashboard requesting analytics

```
Interface: Dashboard
Query:
  query_type:   "ListAnalytics"
  parameters:   { channel_id: "hypogeum", source: "api",
                  date_range: "last_30_days" }
  requested_by: "operator:muhamed"
Core response:
  a direct snapshot of already-collected analytics rows
  (source='api' only — mock data is never returned as a Query result,
  per the platform's existing data-integrity contract)
Dashboard then:
  renders the snapshot as a chart. No Command is involved: nothing is
  triggered, nothing changes state.
```

## CLI requesting production

```
Interface: CLI  (run_pipeline.py, emergency/debugging path)
Command:
  command_type:    "ProduceEpisode"
  idempotency_key:  "produce:ep_001"
  payload:          { topic: "The Antikythera Mechanism",
                      episode_id: "ep_001", channel_id: "hypogeum" }
  requested_by:     "operator:muhamed"
Core response:
  execution_id:     "exec_a114"
  initial_state:    "Pending"
CLI then:
  polls Query GetExecutionStatus(execution_id="exec_a114") on an
  interval and prints state transitions to stdout until a terminal
  state (Completed / Failed / Cancelled) is reached.
```

## REST API requesting production

```
Interface: REST API
HTTP:
  POST /v1/executions
  Idempotency-Key: produce:ep_001
  Body: { "command_type": "ProduceEpisode",
          "payload": { "topic_id": "hyp_topic_0042",
                       "channel_id": "hypogeum" } }
  Authorization: Bearer <api_key>   (Section 10: authentication only)
Core response:
  201 Created
  { "execution_id": "exec_9f21", "state": "Pending" }
Caller then:
  GET /v1/executions/exec_9f21   → current state (Section 12 fields)
  or subscribes to a Webhook for ExecutionStateChanged (Section 8).
```

## SDK requesting production

```
Interface: SDK  (typed client wrapping the REST API)
Command:
  command_type:    "ProduceEpisode"
  idempotency_key:  "produce:ep_001"
  payload:          { topic_id: "hyp_topic_0042", channel_id: "hypogeum" }
  requested_by:     "api_key:sdk_client_7"
Core response:
  execution_id:     "exec_9f21"
  initial_state:    "Pending"
SDK then:
  wraps GetExecutionStatus(execution_id="exec_9f21") in a typed method
  (e.g. `execution.wait_for_terminal_state()`), polling or subscribing
  to Events identically to the REST API example — the SDK adds no
  behavior the REST API contract does not already define.
```

## Webhook receiving a production Event

```
Interface: Webhook subscriber (external system)
Prior step:
  subscribed to ExecutionStateChanged Events for channel_id="hypogeum"
  via the same REST API used for the production example above.
Core emits (Section 8):
  POST <subscriber_url>
  { "event_type": "ExecutionStateChanged",
    "execution_id": "exec_9f21",
    "occurred_at": "...",
    "state": "Completed" }
Subscriber then:
  treats this as an observation only (Section 8): it may re-render, or
  it may call GET /v1/executions/exec_9f21 to confirm before acting —
  it must not treat receipt of the Event alone as authorization to
  submit any further Command, and must tolerate a duplicate delivery of
  the same Event without side effects.
```

## Retry, Resume, and Cancellation target an existing Execution

```
Interface: any (Dashboard, CLI, REST API, or SDK — identical contract)
Command:
  command_type:    "RetryExecution"      (or "ResumeExecution" /
                                          "CancelExecution")
  idempotency_key:  "retry:exec_9f21:1"
  payload:          { execution_id: "exec_9f21" }
  requested_by:     "operator:muhamed"
Core response:
  execution_id:     "exec_9f21"          (the same execution — retry/
                                          resume/cancel act on an
                                          existing Execution, they do
                                          not mint a new one)
  state:            "Retrying" | "Running" | "Cancelled"
                     (Section 11's vocabulary; which one the Core
                     applies is a Core/Workflow policy decision per
                     `WORKFLOW_MODEL.md` Failure Handling, never the
                     Interface's choice)
```

Across the five Command examples above (Dashboard, CLI, REST API, and
SDK requesting production; Retry/Resume/Cancel against an existing
execution): the same `command_type` shape, the same payload shape, the
same `execution_id`/state contract, the same authentication-then-
authorization split, and the same terminal states — only the transport
differs. The one Query example (Dashboard requesting analytics) and the
one Event example (Webhook receiving a state change) are held to the
same standard within their own contract type: every Interface's Query
returns the identical snapshot shape (Section 7), and every Interface's
Event carries the identical envelope (Section 8), regardless of
transport. This is the concrete demonstration of Section 17.

---

# Relationship to Other Components

Core

Accepts Commands and Queries from every Interface; the sole owner of
authorization, business validation, and execution.

Services

Never contacted directly by an Interface; coordinated exclusively by
the Core (`SERVICE_MODEL.md`).

Workflows

The sole mechanism by which the Core executes a Command's business
logic (`WORKFLOW_MODEL.md` Authority: no execution path may bypass the
Workflow Model); also produce the observable state (state, stage,
progress) that Query and Event contracts expose.

Infrastructure

Provides the authentication mechanics an Interface uses to verify a
caller, and nothing else that an Interface touches directly.

Channel Packages

Never read or written by an Interface directly; loaded into the
Execution Context by the Core.

---

# Authority

This document defines the official contract between every Interface and
Cortex.

Every Dashboard, CLI, REST API, SDK, Webhook, and future Interface must
communicate with Cortex exclusively through the Command, Query, and
Event contracts defined here.

An Interface implementation that bypasses this contract — by calling a
Service directly, by owning a business decision, or by diverging from
another Interface's outcome for the identical Command — is an
architectural violation and must be corrected before the Interface's
development continues.
