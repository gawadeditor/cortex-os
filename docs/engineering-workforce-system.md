# Engineering Workforce System — Phase B4.95 Design

**Status:** Design only — no implementation  
**Phase:** B4.95 (Factory OS Productization)  
**Date:** 2026-06-13  
**Depends on:** B4.9 (Engineering Director System)  
**Feeds into:** B5 (Dashboard Architecture), B5.25 (Product Experience System)  
**Author:** Factory OS Architect

---

## 1. Problem Statement

Phase B4.9 defined the Engineering Director as the implementation layer between governance decisions and factory state. It owns the full lifecycle: evaluate, plan, approve, execute, validate, report, roll back.

But B4.9 deliberately left one question unanswered: who does the actual work?

The Engineering Director knows *what* needs to change and *whether* it is safe. It does not specialize in *how* to implement a Python API contract, *how* to write a safe database migration, *how* to design a dashboard component that respects the design system, or *how* to validate a deployment configuration.

Phase B4.95 defines the **Engineering Workforce**: six specialized roles that receive work assignments from the Engineering Director and execute implementation within tightly bounded authority.

The relationship is explicit:

| Entity | Responsibility |
|---|---|
| Engineering Director (B4.9) | Plans, approves, coordinates, validates, reports |
| Engineering Workforce (B4.95) | Executes specialized implementation within bounded authority |

The Engineering Director is the general contractor. The Engineering Workforce are the specialists. The general contractor does not lay pipe. The plumber does not pour concrete. Neither does work outside their scope without authorization.

---

## 2. Workforce Architecture

### 2.1 Full System Context

```
Intelligence Systems
  (Learning, Agent Director, PMD, Evolution)
              │
              │  approved_recommendation
              ▼
┌─────────────────────────────────────────────────┐
│              ENGINEERING DIRECTOR (B4.9)         │
│                                                   │
│  evaluate → plan → approve → report → monitor    │
│                  │                               │
│    create_engineering_task()                     │
│    assign_engineering_role()                     │
│    approve_engineering_change()                  │
│    generate_engineering_report()                 │
└──────────────────────┬────────────────────────--┘
                       │  task assignment
                       ▼
┌─────────────────────────────────────────────────┐
│           ENGINEERING WORKFORCE (B4.95)           │
│                                                   │
│   Backend      Frontend     Database              │
│   Engineer     Engineer     Engineer              │
│                                                   │
│   QA           DevOps       UX/UI                 │
│   Engineer     Engineer     Designer              │
│                                                   │
│   submit_role_output()                            │
│   validate_role_output()                          │
│   run_engineering_qa()                            │
└──────────────────────┬────────────────────────--┘
                       │  validated output
                       ▼
┌─────────────────────────────────────────────────┐
│              ACCEPTANCE AUDIT                     │
│                                                   │
│  Immutable record of every engineering change.   │
│  Every completed task generates an audit entry.  │
│  Rolled-back tasks are recorded, not erased.     │
└─────────────────────────────────────────────────┘
```

### 2.2 Relationship to Each System

**Engineering Director → Engineering Workforce**  
The Director creates tasks, assigns roles, receives outputs, and triggers QA. The Workforce never initiates work — it only executes assigned tasks. A role that receives no assignment does nothing.

**Engineering Workforce → Dashboard**  
The Frontend Engineer and UX/UI Designer produce dashboard artifacts. Every dashboard-facing output goes through UX/UI review before QA validation. The Dashboard Backend API contract is defined by the Backend Engineer. The frontend trusts the API; the API trusts nothing.

**Engineering Workforce → Product Experience System (B5.25)**  
The PES defines visual and interaction standards. The UX/UI Designer applies those standards. The Frontend Engineer implements them. The QA Engineer validates them (where automatable). The PES is the rule source; the Workforce is the rule applier.

**Engineering Workforce → Runtime Configuration**  
The Backend Engineer may update runtime configuration values via the Config Layer API (B4). It does not modify `.env` directly. Configuration changes are treated as engineering tasks with full rollback plans.

**Engineering Workforce → Modules**  
The Backend Engineer implements module code changes. The DevOps Engineer handles module deployment lifecycle. The Engineering Director (via Category 5 changes from B4.9) governs module state transitions. The Workforce executes but does not govern.

**Engineering Workforce → Agents**  
The Backend Engineer implements agent code changes (prompt pack updates, skill integration). The Engineering Director governs which agent changes are approved. The QA Engineer validates agent contract compliance before activation. No Workforce role modifies an agent's registry record directly — that is the Engineering Director's write path.

**Engineering Workforce → Acceptance Audit**  
Every completed engineering task generates an audit entry. The Acceptance Audit is write-once from the Engineering Director's perspective. Workforce roles produce artifacts; the Director submits the consolidated change report to the audit. Individual roles do not write to the audit directly.

### 2.3 Workforce Entity Model

Every Workforce role is a first-class entity in the Engineering system:

```
WorkforceRole
├── role_id             string      Stable slug. e.g., "backend-engineer"
├── role_name           string      Display name. e.g., "Backend Engineer"
├── domain_scope        string[]    File types and systems this role is authorized to modify
├── authority_ceiling   RiskLevel   Maximum risk level this role can execute without escalation
├── required_skills     Skill[]     Capabilities this role must be able to invoke
├── collaboration_peers Role[]      Roles whose review is required before output is valid
├── qa_required         boolean     Whether QA review is mandatory (always true for code roles)
└── status              enum        "available" | "busy" | "suspended"
```

A role is "busy" while an assigned task is in progress. A single role cannot have two tasks assigned simultaneously unless the Engineering Director explicitly authorizes parallel execution for non-overlapping scopes.

---

## 3. Role Definitions

### 3.1 Backend Engineer

**Purpose:** Implement Python backend changes to the Factory OS — business logic, API contracts, agent integration, module integration, and data access patterns.

---

**Responsibilities:**
- Python code changes in defined backend scope
- Dashboard Backend API endpoint implementation
- Business logic for factory entity managers (channel_manager, template_manager, module_manager, etc.)
- Agent integration code (how pipeline calls agents, how agents receive channel profile context)
- Module integration code (capability checks, module health callbacks)
- Internal API contract definitions (the typed contracts between backend components)
- Config Layer (B4) integration — reading resolved config, never writing raw config files

**Authority Boundaries:**
- May modify: `.py` files within the defined backend package scope
- May not modify: frontend code, database schema files, migration scripts, deployment scripts, test files (except to fix a failing contract they authored)
- May not write to: agent registry, module registry, channel profile (these go through managers)
- May not call: external production APIs during implementation (use mocks or stubs)
- May not read: raw secrets from `.env` or credential store (must use the secrets access layer)
- Risk ceiling: Medium. High-risk changes require Engineering Director escalation before execution.

**Inputs:**
- Engineering task assignment from Engineering Director
- Implementation plan (change_set from B4.9 plan)
- Relevant entity schemas (from B1–B4.5 design docs)
- API contract specifications from the task
- QA Engineer's test plan (received before implementation, not after)
- UX/UI Designer's API requirements (if the change surfaces to dashboard)

**Outputs:**
- Python source files (new or modified)
- API contract definitions (typed input/output schemas)
- Code-level documentation (only when the WHY is non-obvious — no docstrings for obvious behavior)
- A self-review checklist completed before submit_role_output() is called:
  - [ ] All modified files are within authorized scope
  - [ ] No secrets printed, logged, or hardcoded
  - [ ] No external API calls without mock wrapper
  - [ ] Contract inputs and outputs are typed and validated at system boundaries
  - [ ] Error paths are handled — no silent failures

**Required Skills:**
- Python proficiency (typed, tested, modular)
- Factory OS architecture knowledge (Config Layer, Module System, Agent OS, Run OS)
- API contract design (typed schemas, validation, error codes)
- SQLite data access patterns (reads via manager APIs, not raw SQL in business logic)
- Understanding of the agent execution model (how agents receive context, produce outputs, how gates work)

**What it is not allowed to do:**
- Write frontend code, even if the backend change requires a matching frontend update (that is Frontend Engineer's task — coordinate, do not cross)
- Modify database schema files (coordinate with Database Engineer)
- Approve its own output (QA Engineer validates independently)
- Modify the agent registry, module registry, or provider registry directly (must use manager APIs)
- Generate or display API keys, credentials, or secrets in any output or log
- Make changes that bypass the Config Layer — no direct `.env` reads in new code

**Validation Requirements:**
- QA Engineer runs test suite before output is accepted
- All existing tests must pass (no regressions)
- New behavior must have new tests (submitted with the code, not after)
- API contract changes must pass contract validation tests
- Any change to agent context injection must pass agent contract tests

---

### 3.2 Frontend Engineer

**Purpose:** Implement the Factory OS dashboard — UI components, page wiring, forms, state handling, and API integration against the Dashboard Backend API.

---

**Responsibilities:**
- Dashboard UI implementation (Streamlit in B5 Phase A–D; React in B5 Phase E–H)
- Component implementation from UX/UI Designer specifications
- Page routing and navigation implementation
- Form design and validation (client-side validation only — backend validates all inputs)
- State management (run status polling, loading states, optimistic UI)
- Dashboard Backend API integration (typed API client, error handling, retry logic)
- Responsive layout implementation within PES guidelines (B5.25)
- White-label variable implementation (CSS variables, theme tokens)

**Authority Boundaries:**
- May modify: frontend source files (`.py` Streamlit, `.tsx`/`.ts` React, CSS/styling), dashboard API client
- May not modify: backend Python logic, database schema, migration scripts, deployment scripts
- May not call: external APIs directly from the frontend (all external data goes through the Dashboard Backend API)
- May not expose: secrets, credentials, or internal system identifiers in the UI
- Risk ceiling: Medium. Changes that affect the approval flow, gate verdict display, or cost displays require UX/UI review before implementation begins.

**Inputs:**
- Engineering task assignment
- UX/UI Designer's component specification (required before any UI implementation begins — see §5)
- Dashboard Backend API contract (from Backend Engineer)
- PES standards document (B5.25) — the design system this implementation must conform to
- QA Engineer's acceptance criteria

**Outputs:**
- Frontend source files (components, pages, layouts, API client)
- A self-review checklist completed before submit_role_output():
  - [ ] All data displayed comes from the Dashboard Backend API — no direct internal reads
  - [ ] No secret or credential data rendered (even partially) in the UI
  - [ ] Dangerous actions have confirmation dialogs with stated consequences
  - [ ] All forms validate input before submission AND handle API validation errors gracefully
  - [ ] Navigation and permissions enforced at the API layer — UI is not the trust boundary
  - [ ] PES component guidelines followed — no ad-hoc styling outside design system

**Required Skills:**
- Streamlit (Phase B5A–D) or React/TypeScript (Phase B5E–H)
- Dashboard Backend API integration patterns (typed client, error handling)
- Factory OS data model (what entities exist, their states, their relationships)
- Dashboard safety rules (§8 of B5) — must implement with full awareness of what the backend enforces
- PES design system (B5.25) — must implement to system specifications, not personal preference
- Accessibility fundamentals (keyboard navigation, ARIA, color contrast)

**What it is not allowed to do:**
- Implement logic that bypasses the Dashboard Backend API (no direct database reads from the frontend)
- Expose raw entity IDs or internal system details in URLs or UI text that could be used to probe the API
- Implement a UI state where a dangerous action could be triggered without a confirmation step
- Design new components without UX/UI Designer specification (may implement against existing spec; may not invent new patterns)
- Approve its own output

**Validation Requirements:**
- QA Engineer validates against PES standards and acceptance criteria
- Component-level UI tests (where automated testing is feasible in the current tech stack)
- All existing frontend tests pass
- UX/UI Designer reviews the implemented output against their specification (catch deviations early)
- Accessibility: at minimum, keyboard navigation of all interactive elements must be verifiable

---

### 3.3 Database Engineer

**Purpose:** Manage all Factory OS database schema, migrations, indexes, views, and data integrity constraints. The single authority on the structure of stored data.

---

**Responsibilities:**
- SQLite schema design and modification
- Migration script authorship (every schema change is a migration — no ad-hoc `ALTER TABLE` in application code)
- Index design for query performance
- View definitions for commonly joined queries
- Data integrity constraints (NOT NULL, FOREIGN KEY, CHECK constraints)
- Query performance analysis (explain query plans for slow paths)
- Data access policy (what tables can be read by which managers — enforced at manager layer, designed here)
- Migration rollback scripts (every migration ships with a down-migration)

**Authority Boundaries:**
- May modify: SQL schema files, migration scripts, index and view definitions
- May not modify: Python application code, frontend, deployment scripts
- May not run: destructive migrations against production data without a committed, tested rollback script
- May not remove: columns or tables that are referenced by any active application code (coordinate with Backend Engineer first)
- Risk ceiling: High. Any migration that drops columns, tables, or changes column types is always a High-risk change requiring Engineering Director approval and a committed rollback before the migration runs.

**Inputs:**
- Engineering task assignment
- Backend Engineer's data access requirements (what the backend needs to read and write)
- Current schema state (must read before proposing changes)
- Query performance profiles (where applicable)

**Outputs:**
- SQL schema files (CREATE TABLE, ALTER TABLE, index definitions, view definitions)
- Migration scripts: always a pair — `up_migration.sql` and `down_migration.sql`
- Data integrity documentation (what constraints enforce which business rules)
- A self-review checklist completed before submit_role_output():
  - [ ] Every schema change has a paired rollback migration
  - [ ] Rollback migration tested on a copy of current schema before submitting
  - [ ] No column or table removed without Backend Engineer confirming zero active references
  - [ ] All new NOT NULL columns have a DEFAULT or are added in a two-step migration (add nullable → backfill → add constraint)
  - [ ] Index names are descriptive and follow naming convention
  - [ ] No migration runs `DROP TABLE` without explicit Engineering Director approval

**Required Skills:**
- SQLite schema design (type affinity, constraint syntax, index strategy)
- Migration sequencing (why migrations must be additive before they are subtractive)
- Data integrity modeling (when to use FK constraints, when to enforce in application layer)
- Query analysis (`EXPLAIN QUERY PLAN` interpretation)
- Factory OS data model (all existing tables and their business meaning)

**What it is not allowed to do:**
- Run migrations against production data without a rollback plan committed and tested first
- Remove any schema element without coordination with the Backend Engineer
- Write application business logic (DB Engineer writes structure, Backend Engineer writes behavior)
- Approve its own migrations
- Create any migration that cannot be rolled back completely (if true irreversibility is required, it must be flagged as such and human-approved with explicit acknowledgment)

**Validation Requirements:**
- QA Engineer validates migration on a test copy of the database
- Rollback migration tested immediately after up-migration (not deferred)
- All existing database-backed tests pass after migration
- Backend Engineer confirms schema change matches their data access requirements

---

### 3.4 QA Engineer

**Purpose:** Own test plans, regression coverage, acceptance criteria, and safety validation for every engineering change. The QA Engineer is the gate between implementation and activation. Nothing activates without QA sign-off.

---

**Responsibilities:**
- Test plan authorship for every engineering task (produced before implementation begins)
- Writing and maintaining test suites (unit, integration, contract, acceptance)
- Regression coverage analysis (which existing tests are relevant to this change)
- Acceptance criteria definition (what must be true for the change to be considered complete)
- Safety validation (no external calls in tests without mocks, no production data, no secrets in test output)
- Mocking external dependencies (LLM calls, provider APIs, YouTube API — all mocked in tests)
- Full suite gating (the full test suite must pass, not just the new tests)
- Failure triage (when a test fails, QA Engineer determines: test bug, implementation bug, or spec bug)

**Authority Boundaries:**
- May modify: test files, test fixtures, mock definitions, acceptance criteria documents
- May not modify: production code, database schema, deployment scripts, frontend application code
- May not approve: its own test results — QA output is reviewed by Engineering Director, not self-approved
- May not bypass: external call mocking — every external API call in any test must be mocked, no exceptions
- Risk ceiling: Low for test changes. QA changes to production code (fixing a breaking test by changing application code) require Backend Engineer coordination.

**Inputs:**
- Engineering task assignment (received at the same time as the implementing role — QA starts in parallel, not after)
- Implementation plan (change_set from B4.9) — defines what will change
- Role outputs from Backend Engineer, Frontend Engineer, Database Engineer, DevOps Engineer
- Existing test suite (current state)
- Acceptance criteria from the originating recommendation

**Outputs:**
- Test plan document (produced before role implementation begins):
  - Which existing tests are affected
  - Which new tests are required
  - Acceptance criteria (what defines "done")
  - Safety constraints (what must NOT happen during testing)
- New test files (unit, integration, contract, acceptance)
- Updated existing tests where the change legitimately changes expected behavior
- QA result report: `run_engineering_qa()` output (see §8)
- A self-review checklist before submit_role_output():
  - [ ] All external API calls are mocked (no live calls to OpenRouter, ElevenLabs, MUAPI, YouTube, etc.)
  - [ ] No production data used in any test
  - [ ] No secrets or credentials in test output, fixtures, or assertions
  - [ ] Full test suite runs clean (not just new tests)
  - [ ] Regression tests explicitly verify no prior behavior was broken
  - [ ] Acceptance criteria are stated in verifiable terms — not "it works," but "given X input, Y output is produced"

**Required Skills:**
- Python testing (pytest, unittest, fixtures, parametrize, mock)
- Contract testing (validating API schemas at the boundary — inputs and outputs match declared types)
- External dependency mocking (how to mock LLM calls, provider responses, YouTube API responses)
- Factory OS pipeline knowledge (how the production pipeline executes, where gates apply)
- Risk assessment (ability to identify what could go wrong from a given change)
- Test isolation principles (tests must not share state, not depend on order, not require network access)

**What it is not allowed to do:**
- Make live external API calls in any test without explicit Engineering Director authorization
- Approve its own QA report (QA output is reviewed by Engineering Director)
- Modify production application code to make tests pass (if a test reveals a bug, that bug must be fixed by the appropriate implementing role — not hidden by a test change)
- Mark a task as QA-passed when the full suite has not run
- Skip regression testing because "the change was small"

**Validation Requirements:**
- The QA Engineer's own output (test files) is reviewed by the Engineering Director for completeness
- Test coverage of the change must meet the minimum coverage requirement per change category (from B4.9 §6.1)
- The full test suite must run and pass — not only the new tests

---

### 3.5 DevOps Engineer

**Purpose:** Own the deployment, environment configuration, process management, backup, monitoring, and operational readiness of the Factory OS installation. The single authority on how the factory runs as a system process.

---

**Responsibilities:**
- Local deployment scripting (how the factory installs and starts on a developer machine)
- Self-hosted deployment configuration (how the factory runs on a production server — Mac mini, VPS, etc.)
- Environment setup documentation and automation (Python environment, dependencies, required services)
- Process management (how the factory process starts, stops, restarts, recovers from crash)
- Backup configuration (what gets backed up, how often, where)
- Monitoring and alerting configuration (process health, disk usage, API availability)
- Environment variable management (how secrets are injected — never hardcoded, never logged)
- Later: cloud readiness (containerization, cloud hosting — deferred but design must not preclude)

**Authority Boundaries:**
- May modify: deployment scripts, environment configuration files, process management configs, backup scripts, monitoring configs
- May not modify: application code (Python, frontend), database schema, test files
- May not expose: secrets in deployment artifacts — scripts must use environment variable injection, never hardcoded values
- May not modify: production environment without an explicit change task from the Engineering Director
- Risk ceiling: High. Any production environment change is High-risk and requires Engineering Director approval.

**Inputs:**
- Engineering task assignment
- Application code from Backend/Frontend Engineers (to understand what needs to run)
- Database migration outputs from Database Engineer (to sequence correctly in deployment)
- Environmental requirements declared by the application (what services, what ports, what credentials)

**Outputs:**
- Deployment scripts (installation, startup, shutdown, health check)
- Environment configuration templates (`.env.example` with all required variables documented but no values)
- Process management configuration (e.g., systemd unit files, launchd plists, process supervisor configs)
- Backup scripts and schedules
- Monitoring configuration (health check endpoints, alert thresholds)
- A self-review checklist before submit_role_output():
  - [ ] No secrets, credentials, or API keys in any output file
  - [ ] All required environment variables are documented in `.env.example`
  - [ ] Startup script includes a pre-flight check (validates required env vars before attempting start)
  - [ ] Shutdown is graceful — no active runs interrupted mid-execution
  - [ ] Backup script tested on a copy — not only the backup script but the restore path

**Required Skills:**
- Shell scripting (bash, zsh) for deployment automation
- Python environment management (venv, pip, dependency pinning)
- Process management (systemd, launchd, supervisor — whichever applies to the target platform)
- macOS and Linux server administration (current deployment targets)
- SQLite backup strategies (WAL mode, copy-on-write backup)
- Environment variable injection patterns (no secrets in files, inject at runtime)
- Future readiness: basic Docker knowledge (not required now, must not be precluded by current design)

**What it is not allowed to do:**
- Hardcode any credential, API key, or secret value in any output file
- Modify the production environment without an engineering task assigned by the Engineering Director
- Run database migrations as part of a deployment script without a rollback-capable deployment sequence
- Approve its own deployment output (QA Engineer validates deployment configuration; DevOps cannot self-approve)
- Make application behavior decisions (if a deployment issue reveals an application configuration problem, that goes back to Backend Engineer, not DevOps)

**Validation Requirements:**
- QA Engineer validates deployment scripts in a clean environment (fresh install, not the developer's working environment)
- A health check must pass after deployment (not just "the process started")
- DevOps output for any production environment change is reviewed by the Engineering Director before application

---

### 3.6 UX/UI Designer

**Purpose:** Define the information architecture, user flows, component design, and visual standards for the Factory OS dashboard and all operator-facing interfaces. The UX/UI Designer produces specifications — not code. It is the authoritative source for "what the dashboard should be" in terms of human experience.

---

**Responsibilities:**
- Information architecture: how dashboard sections are organized, what goes where, how operators navigate
- User flow design: the sequence of steps to complete any operator task (create channel, approve change, queue run, review audit)
- Design system specification: component rules, spacing, typography, color application — consistent with PES (B5.25)
- Component specification: exactly what a component looks like, what it contains, and how it behaves (for Frontend Engineer implementation)
- Accessibility rules: keyboard navigation paths, ARIA roles, color contrast requirements, focus management
- White-label UX: how the dashboard adapts to different factory branding configurations without losing coherence
- Dashboard consistency: review of all implemented dashboard features to confirm they follow the design system
- Error state design: what the operator sees when something goes wrong — error messages, recovery actions, escalation paths

**Authority Boundaries:**
- May produce: design specifications, wireframes (text-based or otherwise), component documentation, flow diagrams, style definitions
- May not produce: code of any kind (Python, HTML, CSS, TypeScript)
- May not approve: code implementations (approvals belong to QA Engineer and Engineering Director)
- May not bypass: the PES standards document (B5.25) — UX/UI Designer applies PES, does not override it
- Authority to block: UX/UI Designer may flag a Frontend Engineer output as "does not conform to specification" — this is not an approval or rejection, but it is a required input to QA validation for dashboard-facing changes

**Inputs:**
- Engineering task assignment
- PES standards document (B5.25) — the authoritative design system
- Factory OS entity model (what data exists that the dashboard must surface)
- Existing dashboard specifications (for consistency review)
- Operator feedback or usability issues (if any have been surfaced)
- Accessibility requirements (WCAG 2.1 AA as baseline)

**Outputs:**
- Component specification documents:
  - Component name and purpose
  - Visual layout (described precisely in text, or as ASCII wireframe)
  - States (default, hover, active, disabled, loading, error)
  - Contents (what data fields, labels, actions it contains)
  - Behavior (what happens on interaction)
  - Accessibility notes (keyboard behavior, ARIA labels, focus order)
- User flow documents:
  - Step-by-step sequence for completing a task
  - Decision points and their consequences
  - Error handling flows
  - Success states
- Design system contribution: if a new pattern is needed that is not in PES, UX/UI Designer proposes a PES addition (not implements independently)
- Conformance review: after Frontend Engineer submits output, UX/UI Designer reviews and notes any deviations from specification

**Required Skills:**
- Information architecture (how to organize complex data for operator usability)
- Interaction design (flows, states, progressive disclosure, error recovery)
- Accessibility (WCAG 2.1 AA principles, practical implementation patterns)
- Factory OS domain knowledge (what all the entities are, what operators need to do with them)
- White-label design thinking (design systems that adapt without breaking)
- Text-based design communication (this system operates without graphic design tools — specifications must be precise enough to implement without visual mockups)

**What it is not allowed to do:**
- Write code of any kind — specifications only
- Override PES standards — may propose additions or changes to PES, but may not implement deviations unilaterally
- Approve Frontend Engineer implementations (that is QA's role — UX/UI Designer reviews for specification conformance, QA approves)
- Block a change from the Engineering Director without a specific specification violation — opinion-based blocks are not valid
- Invent new design patterns without documenting them for PES inclusion

**Validation Requirements:**
- UX/UI Designer output (specifications) is reviewed by the Engineering Director for completeness before Frontend Engineer begins implementation
- After Frontend Engineer submits output, UX/UI Designer reviews for specification conformance and submits a conformance report (pass | pass_with_notes | does_not_conform)
- QA Engineer validates automatable accessibility criteria

---

## 4. Execution Flow

This is the full execution flow from approved change to audit record. It integrates the Engineering Director (B4.9) and the Engineering Workforce (B4.95).

```
┌─────────────────────────────────────────────────────────────────────┐
│  ENGINEERING DIRECTOR: Approved change received                      │
│  (from Learning System, Agent Director, PMD, or Evolution System)   │
│                                                                       │
│  Step 1: evaluate_change() — classify risk, category, dependencies  │
│  Step 2: generate_implementation_plan() — define change_set         │
│  Step 3: create_engineering_task() — create task record             │
│  Step 4: assign_engineering_role() — assign role(s) to task        │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ task assignment
          ┌─────────────────┼──────────────────────────┐
          │                 │                           │
          ▼                 ▼                           ▼
   [QA Engineer]     [Implementing Role]          [UX/UI Designer]
   Receives task      Receives task               Receives task
   in parallel        in parallel                 (if dashboard-facing)
          │                 │                           │
          ▼                 │                           │
   Produces test plan       │                           │
   (before implementation)  │                           ▼
          │                 │                  Produces component spec
          │                 │                  (before implementation)
          │                 ▼                           │
          │         [Implementation begins]             │
          │         Implementing role reads:            │
          │           - Engineering Director's plan     │
          │           - QA Engineer's test plan  ◄──────┘
          │           - UX/UI spec (if applicable)
          │                 │
          │                 ▼
          │         [Implementation complete]
          │         Role calls submit_role_output()
          │                 │
          ▼                 ▼
   [QA Engineer receives output]
   run_engineering_qa()
      - Run new tests
      - Run full regression suite
      - Validate against acceptance criteria
      - Mock all external calls
          │
     ┌────┴────────────────┐
     │                     │
     ▼ PASS                ▼ FAIL
     │               [Returned to implementing role]
     │               Failure detail attached
     │               Role fixes and resubmits
     │               (max 3 resubmit cycles before escalation)
     │
     ▼
[UX/UI Designer conformance review] (dashboard-facing only)
submit_role_output() → conformance_report
     │
     ├── does_not_conform → returned to Frontend Engineer
     │
     ▼ conforms (or not dashboard-facing)
     │
[ENGINEERING DIRECTOR: validate_change()]
     - Validates test results meet minimum requirements
     - Confirms rollback plan integrity
     - Confirms no safety-classified fields changed without human approval
     │
     ├── [human approval required] → Dashboard review gate
     │
     ▼ approved
[Engineering Director: approve_engineering_change()]
     - Activates change in staging state
     - Begins monitoring window
     │
     ▼
[Engineering Director: generate_engineering_report()]
     - Full modification log
     - All test results
     - QA report
     - UX conformance report (if applicable)
     - Submitted to Acceptance Audit
     │
     ▼
[Acceptance Audit: change recorded — immutable]
```

### Multi-Role Task Sequencing

Some changes require multiple roles. The sequencing rules are:

| Change Type | Sequencing |
|---|---|
| Backend + DB change | DB Engineer first (schema), then Backend Engineer (code). Cannot reverse — code cannot reference tables that don't exist yet. |
| Backend + Frontend change | Backend API first, then Frontend (Frontend needs a stable API contract). QA validates both together. |
| Dashboard UI change | UX/UI spec first, then Frontend Engineer. UX review after Frontend submits. |
| DB + Backend + Frontend change | DB → Backend → Frontend → UX Review → QA |
| DevOps change | Usually independent. If triggered by Backend change, Backend completes first. |
| Prompt-only change | Backend Engineer (apply new prompt pack) → QA. No DB, Frontend, or DevOps involvement. |

Roles in parallel are permitted only when their work is genuinely independent:
- QA writes test plan in parallel with implementation (not after)
- UX/UI Designer writes spec in parallel with Backend Engineer API design (not after)
- DevOps and Backend can work in parallel if their changes are in non-overlapping scopes

---

## 5. Collaboration Rules

These rules govern how roles interact. Violations require Engineering Director intervention.

**Rule 1 — Scope confinement**  
No role may modify files or systems outside its defined domain scope without written Engineering Director authorization in the task assignment. "It was easier to fix it here" is not authorization.

**Rule 2 — UX/UI Designer reviews all dashboard-facing changes**  
Any change that produces, modifies, or removes a dashboard UI element must have a UX/UI Designer conformance review as part of the task. This is not optional. If the UX/UI Designer is not assigned to the task and a dashboard element is affected, the task must be paused and the Designer assigned before Frontend submits output.

**Rule 3 — QA Engineer must validate all changes before completion**  
No change can be marked complete without a passing QA result (`run_engineering_qa()` returning `PASS`). The Engineering Director cannot call `approve_engineering_change()` without a QA pass record attached to the task. No exceptions — not for "small" changes, not for "urgent" changes, not for "obviously correct" changes.

**Rule 4 — Database Engineer reviews schema and migration changes**  
Any change that modifies a database schema file or migration script requires Database Engineer review, even if the change was drafted by another role. Incidental schema changes (Backend Engineer adds a column without formal task) are not permitted — they must be converted to a formal DB task.

**Rule 5 — DevOps Engineer reviews deployment-related changes**  
Any change that affects how the factory starts, stops, restarts, or is installed requires DevOps Engineer review. This includes changes to environment variable handling, process management, and any script that runs outside the Python application process.

**Rule 6 — Engineering Director owns the change report**  
The final report submitted to the Acceptance Audit is produced by the Engineering Director, not by any individual role. Individual roles produce their own outputs; the Director consolidates them. No role submits directly to the audit.

**Rule 7 — Resubmit limit**  
If a role's output fails QA and is returned for revision, the role may resubmit a maximum of 3 times. If the third resubmission also fails, the task is escalated to the Engineering Director for re-evaluation. The Director may: extend the resubmit limit, reassign the task, simplify the change scope, or reject the change.

**Rule 8 — No role blocks without citing a rule**  
A role review that results in "does not conform" or "fails QA" must cite the specific rule, standard, or requirement that was violated. Subjective blocks are not valid. "I don't like the approach" is not a valid block. "This violates PES typography rule 3.2" is.

---

## 6. Safety Rules

**Rule S1 — No production run during engineering modification**  
When any engineering task is in `EXECUTING` state for the affected factory component, the Run OS must block new runs that use that component. The Engineering Director sets a component lock at task start; the Run OS enforces it. Existing in-progress runs complete; new runs queue until the lock is released.

**Rule S2 — No external API calls in implementation or testing without authorization**  
During implementation and testing, no real calls to OpenRouter, ElevenLabs, MUAPI, YouTube, Meta, TikTok, or any external service. All external dependencies are mocked. The QA Engineer is responsible for enforcing this in tests. The Backend Engineer is responsible for enforcing this in implementation (use dependency injection for external clients — do not call them directly in business logic).

**Rule S3 — No secrets printed, logged, or exported**  
No role may produce any output that contains or displays a secret, credential, API key, or token. This applies to: code (no hardcoded secrets), test fixtures (no real credentials), log statements (no `print(api_key)`), export files (no credentials in exports). If a role output contains a secret, the output is rejected and the task is suspended pending Security review.

**Rule S4 — No destructive migration without rollback**  
The Database Engineer may not submit any migration script that removes, renames, or restructures data without a tested, complete, passing down-migration. "We can re-add it later" is not a rollback. The rollback must restore the exact prior state.

**Rule S5 — No dashboard UX change without UX/UI review**  
Any change to the dashboard that creates, modifies, or removes UI elements requires UX/UI Designer review (see Collaboration Rule 2). This is also a safety rule: dashboard elements that display gate verdicts, approval statuses, cost data, or dangerous actions must be reviewed for clarity, accuracy, and accessibility before activation.

**Rule S6 — No commit unless targeted tests and full suite pass**  
An engineering task is not complete until both conditions are true:
1. Tests written specifically for the change pass
2. The full existing test suite passes

A task where new tests pass but regression tests fail is a failing task. The implementing role must resolve the regression before resubmitting.

**Rule S7 — No self-modifying code without explicit Engineering Director approval**  
Code that reads, writes, or modifies its own source files, configuration, or schema at runtime is self-modifying. This is permitted only in explicitly designed systems (the Engineering Director itself, the Config Layer) and only with Engineering Director approval. No role may introduce ad-hoc self-modification as a convenience.

---

## 7. Dashboard Visibility

The Engineering Workforce section of the dashboard is a sub-section within the Engineering Director panel (defined in B4.9 §8). It provides task-level visibility into what each role is doing.

### Workforce Task Panel

```
ENGINEERING WORKFORCE — ACTIVE TASKS
──────────────────────────────────────────────────────────────────────
TASK eng_task_004  [Model Change — Research Director]  Medium Risk
──────────────────────────────────────────────────────────────────────
Status:        QA Validation  ●●●○
Assigned to:   Backend Engineer
QA assigned:   QA Engineer
Files changed: hypogeum/agent_registry.py (+12 lines, -3 lines)
               hypogeum/config.py (+1 line)

Tests required:
  ✓ test_agent_registry_model_swap
  ✓ test_config_resolution_model_override
  ○ full_suite  (in progress)

Approval status:   Auto-approved (risk: low, confidence: 0.91)
Rollback status:   Checkpoint chk_004 committed
Last updated:      2026-06-13 10:47 UTC  [View full log]

──────────────────────────────────────────────────────────────────────
TASK eng_task_003  [Dashboard — Add Engineering Director Section]  Low Risk
──────────────────────────────────────────────────────────────────────
Status:        UX Review  ●●●○
Assigned to:   Frontend Engineer
UX assigned:   UX/UI Designer
Files changed: dashboard/pages/engineering.py (+187 lines)
               dashboard/components/change_card.py (+54 lines)

UX conformance: REVIEWING  (submitted 12 min ago)

Tests required:
  ✓ test_engineering_page_renders
  ✓ test_change_card_states
  ✓ full_suite
QA result:     PASS  (08 min ago)

Approval status:   Awaiting UX conformance result
Rollback status:   Checkpoint chk_003 committed
Last updated:      2026-06-13 10:39 UTC  [View full log]

──────────────────────────────────────────────────────────────────────
[View completed tasks]  [View failed tasks]  [View full task history]
```

### Task Detail View

Accessible via `[View full log]` on any task:

```
TASK  eng_task_004  —  Full Log
──────────────────────────────────────────────────────────────────────
Source recommendation:   rec_model_007  (Agent Director)
Approved by:             auto (confidence: 0.91)
Change category:         Model Change
Risk level:              Low

TIMELINE
  10:22  Task created by Engineering Director
  10:22  Assigned: Backend Engineer + QA Engineer
  10:23  QA Engineer: test plan produced (2 tests, 1 full suite check)
  10:24  Backend Engineer: implementation began
  10:31  Backend Engineer: submit_role_output() — 2 files changed
  10:31  QA Engineer: run_engineering_qa() started
  10:38  QA: test_agent_registry_model_swap    PASS
  10:39  QA: test_config_resolution_model_override    PASS
  10:41  QA: full_suite    (3574 tests)    IN PROGRESS ●●○

FILES CHANGED
  hypogeum/agent_registry.py      +12  -3    [View diff]
  hypogeum/config.py              +1   -0    [View diff]

ROLLBACK
  Checkpoint:  chk_004 (committed at 10:22)
  Rollback plan:  2 steps (reverse of change_set)
  Rollback validated:  YES (dry-run at plan time)
  [Trigger manual rollback]

REQUIRED APPROVALS
  Engineering Director:  ✓ Auto-approved
  Human review:          Not required
```

### Final Report View

After task completes, the engineering report is accessible:

```
ENGINEERING REPORT  eng_report_003
──────────────────────────────────────────────────────────────────────
Task:            eng_task_003
Source rec:      rec_dashboard_012
Change summary:  Added Engineering Director panel to dashboard.
                 3 new components: ChangeCard, TaskList, RollbackButton.
                 Connected to Dashboard Backend API /engineering/* endpoints.

Modification log:
  dashboard/pages/engineering.py          CREATED    187 lines
  dashboard/components/change_card.py     CREATED    54 lines
  dashboard/components/task_list.py       CREATED    38 lines

Test results:
  test_engineering_page_renders       PASS
  test_change_card_states             PASS
  test_task_list_filters              PASS
  full_suite (3574 tests)             PASS

QA result:      PASS    (QA Engineer, 2026-06-13 10:55)
UX conformance: PASS    (UX/UI Designer, 2026-06-13 10:48)
                Notes: "Change card loading state matches spec. Rollback button
                         color uses danger-red from PES. Conforming."

Activated:      2026-06-13 10:58 UTC
Monitoring:     5 runs / 48 hours
Status:         CONFIRMED  (2026-06-15 11:22 UTC)
Audit entry:    audit_eng_003  [View in Acceptance Audit]
──────────────────────────────────────────────────────────────────────
```

---

## 8. API Contracts

These are logical contracts — not implementation specs. They define the interface the Engineering Workforce system exposes.

---

### `create_engineering_task()`

```
create_engineering_task(
    plan_id:                string,        # from Engineering Director plan (B4.9)
    task_description:       string,        # plain language: what this task does
    change_category:        ChangeCategory,
    required_roles:         WorkforceRole[],   # which roles are needed
    priority:               "urgent" | "normal" | "deferred",
    parallel_allowed:       boolean,       # can roles work simultaneously
    component_lock:         EntityRef[],   # which entities to lock during execution
    rollback_plan_id:       string         # must reference a committed rollback plan
) → EngineeringTask{
    task_id:                string,
    plan_id:                string,
    status:                 "created",
    assigned_roles:         WorkforceRole[],
    component_locks:        EntityRef[],
    created_at:             datetime
}
```

---

### `assign_engineering_role()`

```
assign_engineering_role(
    task_id:                string,
    role:                   WorkforceRole,
    context:                RoleContext{
        implementation_plan:    ImplementationPlan,
        related_specs:          DocumentRef[],     # PES docs, API contracts, UX specs
        constraints:            string[],          # role-specific constraints for this task
        acceptance_criteria:    string[]           # what done looks like for this role
    }
) → RoleAssignment{
    assignment_id:          string,
    task_id:                string,
    role:                   WorkforceRole,
    status:                 "assigned",
    context:                RoleContext,
    assigned_at:            datetime,
    max_resubmits:          int            # default 3
}
```

---

### `submit_role_output()`

```
submit_role_output(
    assignment_id:          string,
    output_type:            "code" | "schema" | "migration" | "test" |
                           "spec" | "deployment" | "conformance_review",
    artifacts:              Artifact[]{
        artifact_id:            string,
        artifact_type:          string,
        path:                   string,      # relative path of the changed file
        change_type:            "created" | "modified" | "deleted",
        lines_added:            int,
        lines_removed:          int,
        diff_summary:           string       # brief plain-language summary of changes
    },
    self_review_checklist:  ChecklistResult{   # role completes before submitting
        all_items_checked:      boolean,
        any_item_failed:        boolean,
        failed_items:           string[]
    },
    notes:                  string | null
) → RoleOutput{
    output_id:              string,
    assignment_id:          string,
    status:                 "submitted" | "rejected_self_review_failed",
    # If self_review_checklist.any_item_failed → status = "rejected_self_review_failed"
    # Role must resolve checklist failures before Engineering Director receives output
    artifacts:              Artifact[],
    submitted_at:           datetime
}
```

---

### `validate_role_output()`

```
validate_role_output(
    output_id:              string,
) → ValidationResult{
    validation_id:          string,
    output_id:              string,
    scope_check:            ScopeCheckResult{
        files_within_scope:     boolean,
        out_of_scope_files:     string[]    # empty if scope_check passed
    },
    secrets_check:          SecretsCheckResult{
        no_secrets_detected:    boolean,
        secrets_found_at:       string[]    # file paths — empty if clean
    },
    self_review_status:     "complete" | "incomplete" | "failed",
    validation_status:      "pass" | "fail",
    failure_reasons:        string[],
    validated_at:           datetime
}
```

---

### `run_engineering_qa()`

```
run_engineering_qa(
    task_id:                string,
    output_ids:             string[],       # all role outputs being validated together
) → QAResult{
    qa_result_id:           string,
    task_id:                string,
    tests_run:              TestResult[]{
        test_id:                string,
        test_name:              string,
        required:               boolean,
        passed:                 boolean,
        duration_ms:            int,
        failure_detail:         string | null
    },
    full_suite_passed:      boolean,
    new_tests_passed:       boolean,
    regression_tests_passed: boolean,
    external_calls_mocked:  boolean,        # false if any test made a live external call
    qa_verdict:             "PASS" | "FAIL",
    failure_summary:        string | null,
    resubmit_count:         int,            # how many resubmits this task has had
    qa_completed_at:        datetime
}
```

---

### `approve_engineering_change()`

```
approve_engineering_change(
    task_id:                string,
    approved_by:            string,         # "auto" | user_id
    qa_result_id:           string,         # must reference a PASS QA result
    ux_conformance_id:      string | null,  # required if dashboard-facing
) → ApprovalRecord{
    approval_id:            string,
    task_id:                string,
    approved_by:            string,
    qa_result_id:           string,
    ux_conformance_id:      string | null,
    approved_at:            datetime,
    change_activated_at:    datetime | null   # set after activation completes
}
```

---

### `reject_engineering_change()`

```
reject_engineering_change(
    task_id:                string,
    rejected_by:            string,         # user_id (human rejections only)
    reason:                 string,         # specific reason — must cite rule or requirement
    resolution_required:    string          # what would need to change for this to be approvable
) → RejectionRecord{
    rejection_id:           string,
    task_id:                string,
    rejected_by:            string,
    reason:                 string,
    resolution_required:    string,
    rejected_at:            datetime,
    task_status_set_to:     "rejected"      # terminal state
}
```

---

### `generate_engineering_report()`

```
generate_engineering_report(
    task_id:                string,
) → EngineeringReport{
    report_id:              string,
    task_id:                string,
    plan_id:                string,
    recommendation_id:      string,
    change_summary:         string,
    change_category:        ChangeCategory,
    role_outputs:           RoleOutput[],
    modification_log:       ModificationRecord[],
    qa_result:              QAResult,
    ux_conformance:         ConformanceReport | null,
    approval_record:        ApprovalRecord | null,
    rejection_record:       RejectionRecord | null,
    activation_timestamp:   datetime | null,
    monitoring_criteria:    MonitoringCriteria,
    final_status:           "confirmed" | "rolled_back" | "rejected" | "failed",
    rollback_record:        RollbackResult | null,
    submitted_to_audit:     boolean,
    audit_entry_id:         string | null,
    generated_at:           datetime
}
```

---

## 9. Integration with Product Experience System (B5.25)

The Product Experience System (PES) and the Engineering Workforce have a defined relationship. The PES is a design standard; the Workforce is an implementation system. They do not overlap — they connect at defined handoff points.

### The PES-Workforce Chain

```
Product Experience System (B5.25)
  Defines:
    - Dashboard design system (typography, spacing, color, component library)
    - Interaction patterns (how operators navigate, confirm, undo)
    - Accessibility requirements (WCAG 2.1 AA)
    - White-label adaptation rules
    - Mobile / responsive breakpoints
    - Error state standards
          │
          │  reads PES standards as authoritative rules
          ▼
UX/UI Designer (Engineering Workforce)
  Applies:
    - PES standards to each dashboard component specification
    - PES patterns to new user flows
    - PES color/typography tokens in component specs
    - PES accessibility rules in keyboard navigation design
    - Proposes PES additions when new patterns are required
          │
          │  hands specification to Frontend Engineer
          ▼
Frontend Engineer (Engineering Workforce)
  Implements:
    - Component specifications from UX/UI Designer
    - PES CSS/styling tokens (not custom ad-hoc values)
    - Accessible markup as specified
    - Responsive layout per PES breakpoints
          │
          │  submits for validation
          ▼
QA Engineer (Engineering Workforce)
  Validates:
    - Implementation matches specification (where automatable)
    - Accessibility: keyboard navigation, focus order, ARIA (manual review + automated checks)
    - Color contrast ratios (automated)
    - Component state completeness (loading, error, empty states exist)
    - No ad-hoc styling that bypasses PES tokens
```

### Authority Separation

| Decision | Owner |
|---|---|
| What the design system IS | Product Experience System (B5.25) |
| How to apply the design system to a specific dashboard feature | UX/UI Designer |
| How to implement the UX/UI Designer's specification in code | Frontend Engineer |
| Whether the implementation matches the specification | QA Engineer |
| Whether the specification deviates from PES standards | UX/UI Designer (cites PES) |
| Whether a new PES pattern is needed | UX/UI Designer (proposes) → PES (decides) |

**Important:** The UX/UI Designer does not own PES. The UX/UI Designer applies PES. If a dashboard feature requires a pattern not in PES, the Designer proposes the pattern to PES and documents it temporarily as a "PES candidate" in the specification. The Frontend Engineer implements it as specified; the PES system formally adopts it or amends it in the next PES update cycle. The Workforce does not wait for PES adoption before implementing — it implements against the candidate spec.

---

## 10. Risks and Mitigations

### Risk 1 — Too many roles creating coordination overhead

**Failure mode:** A simple change (e.g., update a budget ceiling) requires task creation, role assignment, QA plan, QA execution, conformance review, Engineering Director report, and audit submission. What was a 2-minute config edit becomes a 45-minute governance procedure. Operators perceive the system as bureaucratic and begin bypassing it.

**Mitigation:**

- **Risk-proportionate process depth.** Low-risk changes with auto-approval execute with a streamlined flow: Backend Engineer + QA only. UX/UI review is only required for dashboard-facing changes. DB Engineer is only required when schema is involved. DevOps is only required for deployment changes. The full six-role orchestration is the worst case — most changes involve two or three roles.
- **Parallel execution.** QA Engineer writes the test plan while the implementing role writes code. UX/UI Designer writes the spec while Backend Engineer defines the API. Parallelism reduces wall-clock time even when multiple roles are involved.
- **Configuration-class changes have a fast path.** Configuration-only changes (channel profile updates, schedule changes, budget adjustments) that touch zero code files bypass the Backend/Frontend/DB roles entirely. They go: Engineering Director plan → Backend Config Manager API → QA validation → activate.

---

### Risk 2 — Role overlap (who owns the boundary?)

**Failure mode:** The Backend Engineer fixes a CSS bug while implementing an API. The Frontend Engineer adds a small validation function inline in a backend file for convenience. Neither is explicitly prohibited and both seem reasonable in the moment. Over time, the boundary collapses and the role system loses meaning.

**Mitigation:**

- **Domain scope is defined as file type and path.** The Backend Engineer's scope is `.py` files in the backend package. The Frontend Engineer's scope is frontend source files. If a Backend Engineer commits a change to a `.tsx` file, `validate_role_output()` fails the scope check — the output is rejected regardless of whether the change was correct.
- **Violations are logged, not silently accepted.** The scope check in `validate_role_output()` logs every out-of-scope file in the output. This creates a visible record of drift. If the same role exceeds scope more than twice in 30 days, the Engineering Director reviews the role's authority boundary definition and clarifies.
- **Cross-scope coordination is a task split.** If implementing a feature requires both Backend and Frontend changes, those are two separate task assignments within the same engineering task — not one role doing both. The Engineering Director creates a multi-role task and sequences the assignments.

---

### Risk 3 — Unsafe autonomous code changes

**Failure mode:** The Engineering Workforce can modify Python files. Over time, the system modifies more and more of its own code. There is no human in the loop on low-risk changes. Eventually the factory's own production pipeline is modified autonomously, introducing a subtle regression that degrades content quality over weeks before it is detected.

**Mitigation:**

- **Production pipeline files are blocked from autonomous modification.** The Engineering Workforce's authorized scope explicitly excludes the episode production pipeline files (the agent files that directly generate content: VPD, script engine, narrator, etc.). Changes to production pipeline code are always `high` or `critical` risk — always human-approved.
- **The risk ceiling for each role is enforced.** No role can execute a change at a risk level above its ceiling without Engineering Director escalation. Backend Engineer ceiling is `medium` — a `high`-risk change assigned to Backend Engineer triggers re-evaluation and human approval.
- **The Acceptance Audit provides visibility.** Every autonomous change is recorded immutably. The audit trail is always readable by the operator and can be reviewed at any time. Trend analysis on autonomous changes (frequency, scope, affected components) is surfaced in the Analytics module.
- **Monitoring window catches regressions.** Every change enters a monitoring window. If a production pipeline metric degrades post-change, rollback is triggered.

---

### Risk 4 — Weak QA gate

**Failure mode:** QA is bypassed because a change is "obviously correct." Or: the QA Engineer's tests are weak — they test the happy path but miss edge cases. Or: QA runs tests but the full suite isn't actually run (only the new tests). Over time, QA becomes a rubber stamp.

**Mitigation:**

- **`approve_engineering_change()` requires a QA result ID.** Structurally, the Engineering Director cannot approve a change without a QA result attached. The API rejects the call if `qa_result_id` is null. This is not a convention — it is an API contract constraint.
- **QA result must show `full_suite_passed: true`.** A QA result where `full_suite_passed: false` does not qualify as a pass, even if all new tests passed. The Engineering Director validates this before calling `approve_engineering_change()`.
- **External call detection is automated.** `run_engineering_qa()` includes an `external_calls_mocked` field. If any test during the QA run made a live network call, this is `false`. An `external_calls_mocked: false` QA result is automatically a FAIL, regardless of test pass rates.
- **QA output is reviewed by Engineering Director.** The QA Engineer cannot approve its own work. The Engineering Director validates the QA report before approving the change. It checks: was the test plan followed? Were the required tests run? Were edge cases addressed?

---

### Risk 5 — Dashboard UX drift

**Failure mode:** Five dashboard features are implemented by five different Frontend Engineers over six months. Each interprets the design system slightly differently. The dashboard accumulates inconsistent spacing, slightly different button styles, varying error message formats, and slightly different loading state behaviors. The dashboard no longer feels like a coherent product.

**Mitigation:**

- **UX/UI Designer conformance review is mandatory for all dashboard-facing changes.** This is Collaboration Rule 2 and Safety Rule S5. No dashboard-facing change can be approved without a conformance report from the UX/UI Designer.
- **The PES (B5.25) is the objective standard.** Conformance review is not subjective. The UX/UI Designer cites specific PES rules. A review that says "this looks inconsistent" without citing a rule is not valid. This prevents both drift (inconsistency is cited and fixed) and gatekeeping (vague rejections are invalid).
- **Frontend Engineer self-review includes PES checklist.** The `submit_role_output()` self-review checklist for Frontend Engineer includes "PES component guidelines followed — no ad-hoc styling outside design system." This is checked before submission — the Designer conformance review is a second check, not the first.

---

### Risk 6 — Schema migration damage

**Failure mode:** A database migration drops a column that is still referenced by active code. The migration runs, the column is gone, and every run that tries to read that column throws a database error. Recovery requires a manual restore from backup.

**Mitigation:**

- **DB Engineer scope enforcement.** The Database Engineer cannot merge schema changes without Backend Engineer confirmation that there are zero active code references to the removed element. This confirmation is part of the Database Engineer's self-review checklist.
- **Two-step destructive migration requirement.** Any migration that removes a column or table must be split into two migrations: (1) mark deprecated + stop all reads/writes in application code, (2) remove the schema element in a separate migration after (1) has been in production for a defined period. Single-step destructive migrations are rejected.
- **Migration rollback tested before submit.** The Database Engineer tests the down-migration on a copy of the schema before submitting. The QA Engineer tests the up-and-down cycle in a clean test environment.
- **No migration runs automatically.** Migrations are applied as part of an explicit engineering task — never triggered automatically by the application on startup (no `auto_migrate` flag). Human awareness of each migration is required.

---

### Risk 7 — Deployment breakage

**Failure mode:** A DevOps change (new environment variable, changed startup sequence) causes the factory to fail to start after deployment. The production environment is down. The operator must debug a deployment script while the factory is offline.

**Mitigation:**

- **Deployment scripts include a pre-flight check.** Every startup script validates all required environment variables before attempting to start any factory process. If a required variable is missing or malformed, the script prints a clear diagnostic and exits before starting anything.
- **QA validates in a clean environment.** The DevOps Engineer does not validate in their own working environment — the QA Engineer deploys the script in a fresh environment to confirm it works on a clean install.
- **Startup and shutdown are tested separately.** The QA Engineer runs startup, confirms health, then runs shutdown, confirms clean exit, then runs startup again. This catches stateful issues that only appear on restart.
- **Rollback is a deployment concern, not just a code concern.** Every deployment task includes a rollback deployment procedure. If the new deployment fails health checks, the rollback procedure restores the previous working state.

---

## 11. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B4 — Config Layer | Backend Engineer accesses config via the Config Layer API — never reads `.env` or raw config files directly. DevOps Engineer manages environment variable injection at the deployment layer. |
| B4.5 — Module System | Backend Engineer implements module integration code. DevOps Engineer handles module installation in the deployment sequence. Engineering Director governs module state changes — Workforce executes them. |
| B4.75 — Channel Profile | Backend Engineer implements Channel Manager API changes. Database Engineer handles schema changes for channel profile storage. Configuration changes to channel profiles go through Engineering Director's Category 7 change path. |
| B4.9 — Engineering Director | The Engineering Director is the Workforce's sole source of task assignments. No role executes without a task from the Director. The Director owns the final report; the Workforce owns role-level outputs. |
| B5 — Dashboard Architecture | Frontend Engineer implements the dashboard defined in B5. UX/UI Designer ensures implementations conform to the B5 section specifications. Backend Engineer implements the Dashboard Backend API defined in B5. |
| B5.25 — Product Experience System | UX/UI Designer applies PES standards to all dashboard component specifications. Frontend Engineer implements to those specifications. PES is the rule source; Workforce is the rule applier. |
| B6 — Agent OS | Backend Engineer implements agent integration changes (new agent wiring, contract updates, skill integration). The Engineering Director governs agent registry writes — Backend Engineer implements the code that uses the registry, not the registry entries themselves. |
| B7 — Run OS | The Run OS honors component locks set by the Engineering Director during task execution. No new runs start on a locked component. Existing runs complete; new runs queue. |

---

## 12. Open Questions (Deferred)

1. **Workforce role as AI agent vs. human specialist** — This design treats each role as an execution entity (AI agent with bounded tools and permissions). The design is equally valid if the roles are human specialists. The API contracts and collaboration rules work in both models. The implementation decision (human vs. AI executor) is deferred to the implementation phase.

2. **Parallel task isolation** — When two tasks are running in parallel on different entities, they may both read from the same shared configuration. If one task modifies shared config while the other is reading it, the reading task gets inconsistent data. Full isolation across parallel tasks requires either a transaction model or a read-snapshot approach. Deferred to the implementation phase.

3. **QA Engineer test plan authorship** — The design says QA produces a test plan "before implementation begins." But QA needs to understand what is being built before writing tests. The practical sequence is: Engineering Director shares the implementation plan with QA → QA writes the test plan → implementing role reads both plan and test plan → implementation begins. The timing contract for this handoff needs to be explicit in the implementation phase.

4. **DevOps role scope for cloud readiness** — The DevOps role is scoped to local and self-hosted deployment today. "Later cloud readiness" is noted but not designed. The constraint "design must not preclude cloud" means: no hardcoded file paths as deployment artifacts, no assumptions about local filesystem availability in the application code, environment variable injection everywhere. These are design constraints on Backend Engineer output as much as DevOps output.

5. **UX/UI Designer tooling** — The design says "text-based design communication" because the current system operates without graphic design tools. In a later phase, the UX/UI Designer may use Figma or similar tools to produce specifications. The design should work with or without those tools — the deliverable is always a specification document precise enough to implement, regardless of how it was produced.
