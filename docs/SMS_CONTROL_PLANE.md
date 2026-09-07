# Enterprise SMS Control Plane

A genuinely separate product from Bingo, living in this same repository
at the user's explicit direction (see `DECISIONS.md`, 2026-09-07, for the
full scoping rationale and the honest list of what's deliberately
deferred). This doc covers what's actually built.

## Why this exists

A CTO-level directive asked for a full multi-tenant "SMS Operating
System" — campaign lifecycle, an Android/MacroDroid delivery-node mesh,
a pluggable provider abstraction, compliance, retry/reconciliation, a
real-time ops center, and dozens of other enterprise-SaaS subsystems.
That is honestly a multi-quarter build for a real engineering team. What
shipped instead is one real, fully-tested, end-to-end vertical slice —
schema through a working delivery path through tests — rather than
shallow scaffolding across the whole scope.

## Architecture

```
Admin browser ──▶ web/sms/ (static, /console) ──▶ services/sms/app.py
                                                        │
                                    packages/core/sms/  │  (domain logic)
                                    ├── campaigns.py     │  state machine
                                    ├── messages.py      │  claim/retry/reconcile
                                    ├── nodes.py          │  fleet lifecycle
                                    ├── audience.py       │  recipient resolution
                                    ├── compliance.py     │  suppression list
                                    └── templates.py      │  render + segment count
                                                        │
                                                   Postgres (sms_* tables)
                                                        ▲
Android device (MacroDroid or any HTTP client) ────────┘
        generic node protocol: heartbeat / fetch-job / start / result
```

Authentication, RBAC, and the audit trail are **not** duplicated —
`services/sms/app.py` calls the exact same `services.admin.auth.login()`
/ `resolve_session()` the Bingo admin console uses, checks the same
`services.admin.rbac.PERMISSIONS` dict (extended with `sms:*` keys), and
writes to the same `admin_audit_log` table via `services.admin.audit.record()`.
An admin with a Bingo console account can log into the SMS console with
the identical username/password/TOTP — there is no second account system.

## Domain model

One tenant is seeded by migration `a2ac063da449` (`sms_tenants`, slug
`default`) — `tenant_id` is a real column on every table and genuinely
enforced in every query, but there is no tenant self-service
provisioning yet (nothing to provision a second tenant *for*).

- **`sms_contacts`** — phone, optional display name, a free-form `jsonb`
  attributes bag used for audience targeting.
- **`sms_suppressions`** — a phone in here is excluded from every
  campaign's audience, unconditionally, checked both at campaign
  validation and again at enqueue time.
- **`sms_templates`** — `{{variable}}` placeholders, variables extracted
  and stored at save time.
- **`sms_campaigns`** — the lifecycle state machine (below).
- **`sms_campaign_events`** — every transition, real or system-driven,
  audited here (distinct from `admin_audit_log`, which is who-changed-
  what-admin-setting; this is the campaign's own lifecycle history).
- **`sms_delivery_nodes`** — one row per registered device/agent, a
  per-node hashed credential (never a shared static token), a 0–100
  health score computed from real heartbeat recency + recent delivery
  success rate.
- **`sms_messages`** — one row per (campaign, recipient), immutable
  rendered body, a real `idempotency_key` (`campaign:{id}:{contact_id}`)
  so re-running the enqueue step after a crash can never duplicate a
  message.
- **`sms_delivery_attempts`** — one row per attempt, the audit trail a
  retry/reconciliation decision is made from.

## Campaign lifecycle

```
draft ──validate──▶ ready ──start──▶ running ──pause──▶ paused
  ▲                   │                │  ▲               │
  └────(failed)◀──────┘                │  └───resume───────┘
                                        │
                        (reconciliation, all messages terminal)
                                        ▼
                          completed | completed_with_errors

draft/ready/scheduled/running/paused ──cancel──▶ cancelled
```

Every transition goes through `campaigns._transition()`, which enforces
the allowed FROM-status set and records a row in `sms_campaign_events` —
never an implicit side effect of a bare `UPDATE`. `start_campaign()` is
safely re-callable while already `running` (a genuine crash-recovery
resume, not an error) — the enqueue step is idempotent by construction
(`ON CONFLICT (idempotency_key) DO NOTHING`), so a retried or resumed
start can never create a duplicate message.

`cancel_campaign()` only cancels messages still `queued` — an
already-`assigned`/`sending` message is left to resolve normally, since
you cannot un-send an SMS that may already be in flight.

## The generic delivery-node protocol

Every node (a MacroDroid-driven Android phone today; the protocol never
assumes that) authenticates with a per-node bearer token, issued once at
registration (`POST /nodes`, admin-only, `sms:nodes:manage`) and shown
exactly once — only its SHA-256 digest is ever persisted.

| Call | Who | What |
|---|---|---|
| `POST /v1/nodes/heartbeat` | node | Reports liveness + app version + capabilities; recomputes health score |
| `POST /v1/nodes/fetch-job` | node | Atomically claims the oldest queued message for its tenant (`FOR UPDATE SKIP LOCKED`, priority-ordered); a `pending`/`disabled`/`draining` node gets an honest "no work, and here's why" instead of a job |
| `POST /v1/nodes/jobs/{id}/start` | node | Marks the claimed job as actually being sent (ownership-checked) |
| `POST /v1/nodes/jobs/{id}/result` | node | Reports `delivered` / `failed` / `unknown` + an error class; drives the retry/dead-letter decision |

A **revoked** node cannot even authenticate (the strongest point to
enforce "a revoked node cannot receive work"); a **draining** node
authenticates and heartbeats normally but is never handed new work,
letting any job already in flight on it resolve normally.

## Retry and reconciliation

Six named error classes (`temporary`, `permanent`, `network`, `timeout`,
`node_failure`, `unknown`). `permanent` and an exhausted attempt count
(`MAX_DELIVERY_ATTEMPTS = 3`) both route to `dead_letter` — a real,
operator-visible terminal state, never a silent drop. Everything else is
re-queued (unassigned, so any healthy node can pick it up next).

A background sweep (`services/sms/app.py`'s own `_reconcile_loop`, an
in-process asyncio task — not a separate deployable worker; that
extraction is worth making once volume justifies it, not before) finds
messages sitting in `assigned`/`sending` for more than
`IN_FLIGHT_TIMEOUT` (5 minutes) with no result ever reported, and moves
them through the identical retry-or-dead-letter decision with
`error_class='timeout'` — UNKNOWN_EXECUTION, never blindly assumed
delivered, never blindly resent without accounting for it.

## Compliance

`sms_suppressions` is admin-curated in this pass — there is no inbound
SMS gateway yet to auto-detect a STOP reply. A suppressed phone is
excluded from `resolve_recipients()` unconditionally; a campaign whose
entire audience is suppressed fails validation honestly (`0 resolvable
recipients`) rather than silently succeeding with nothing to send.

## RBAC

`sms:view` (broad, all four roles — matches `payments:view`'s own
breadth reasoning) plus five narrower manage permissions
(`sms:contacts:manage`, `sms:templates:manage`, `sms:campaigns:manage`,
`sms:nodes:manage`, `sms:compliance:manage`, all ops/superadmin) and one
deliberately superadmin-only gate, `sms:campaigns:approve` — the one
action in this whole product that actually sends real messages to real
people, mirroring `payments:approve`/`rooms:emergency_stop`'s own
least-privilege reasoning for the single highest-leverage lever.

## Testing

- `tests/integration/test_sms_core.py` — 29 tests against real Postgres,
  no mocks: audience resolution, suppression, node lifecycle/health
  scoring, the full campaign state machine, message claiming/priority
  ordering, retry classification, and the reconciliation sweep (a real
  backdated `assigned_at`, not a mocked clock).
- `tests/integration/test_sms_app.py` — 9 tests over real HTTP: login
  against a real admin account, RBAC boundaries enforced through the
  actual dependency chain, a full campaign-to-delivery flow through the
  real node protocol, suppression enforcement, and node-ownership
  rejection.
- `tests/integration/test_sms_console_e2e.py` (`pytest -m e2e`) — a real
  Chromium tab driving the actual frontend: login, add a contact,
  register and approve a node, create/validate/start a campaign, and
  confirm the delivered result renders back in the UI.

Each test file that touches the message queue gives itself a fresh,
throwaway tenant (`test_sms_core.py`'s own `tenant_id` fixture) — the
same "polluted shared dev database" failure class already documented
elsewhere in `DECISIONS.md` would otherwise let two tests claim each
other's queued messages in this long-lived, never-truncated dev
database.

## What's deliberately not built yet

See `DECISIONS.md` (2026-09-07) for the full list and reasoning: SMPP/
carrier/HTTP provider adapters, fleet scale beyond a handful of real
nodes, a multi-strategy routing policy engine, automatic inbound
STOP-keyword suppression, a configurable N-person approval workflow,
billing/usage metering and per-tenant quotas, an outbound webhook/domain-
event bus, load/chaos testing at enterprise scale, and tenant
self-service provisioning.
