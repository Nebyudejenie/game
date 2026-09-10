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
                                    ├── templates.py      │  render + segment count
                                    └── csv_import.py     │  bulk CSV -> campaign
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

## Phase 2: node lifecycle, capacity, eligibility, fairness

Added 2026-09-07 in response to a follow-up directive asking for the full
distributed-mesh roadmap (see `DECISIONS.md`'s Phase 2 entry for the full
scoping call — again, one real bounded slice, not the whole roadmap).

**A key architectural finding, not assumed**: the directive's own
"routing engine" concept (least-loaded/round-robin/health-weighted
strategies, explainable per-job node *selection*) describes a **push**
model, where the server picks a node for a job. This protocol is
**pull**: a node asks, the server decides only whether *this asking
node* qualifies and, among qualifying jobs, which one it gets. What
translates onto a pull protocol for real is **eligibility** (does this
node qualify at all) and **fair ordering** (which job it gets first) —
both implemented below. A pluggable multi-strategy routing engine is
deferred until a second real strategy actually needs to exist.

- **Node lifecycle**: `pending → active ⇄ maintenance/disabled/draining →
  revoked`. `maintenance` is a real, admin-settable status distinct from
  `disabled` (different operator meaning: planned work vs. unexplained
  shutdown). `degraded`/`offline` are **never stored** — they're computed
  at read time from `health_score`/heartbeat recency
  (`nodes.py::display_status()`), so there is exactly one source of truth
  for those signals, never a second column that could drift from it.
- **Per-node capacity**: a node advertises `max_concurrent_jobs` via
  heartbeat (defaults to 1 — today's de facto behavior); `fetch-job`
  checks the node's real, live in-flight message count (never a
  self-reported number) before handing out more work.
- **Node-group eligibility**: `sms_campaigns.required_fleet_group`
  (nullable; NULL = any node, today's only prior behavior) restricts a
  campaign's messages to nodes in one exact `fleet_group`.
- **Cross-campaign fairness**: the claim query no longer orders strictly
  by tenant-wide `created_at` (under which one huge campaign starves
  every other campaign queued alongside it). It now ranks each message by
  its own position within its *own* campaign's full message history
  (`ROW_NUMBER() OVER (PARTITION BY campaign_id ORDER BY created_at)`,
  computed over the whole history, not just what's still queued — see
  DECISIONS.md for the real bug this distinction fixes), so campaigns
  interleave rather than serve strictly first-created-first-served.
- **Routing-decision forensics**: every claimed message's
  `sms_delivery_attempts` row now carries a `routing_snapshot` (jsonb):
  the node's fleet_group/health_score/in-flight count at claim time, and
  the campaign's `required_fleet_group` — a real, queryable answer to
  "why did this node get this job."

## Concurrency safety (production-gate audit, 2026-09-07)

`claim_next_message()` is deliberately shaped around two real bugs a
genuine concurrency test found — not theoretical, reproduced 100% of the
time — see DECISIONS.md's audit entry for the full empirical trail:

1. **A CTE-driven `FOR UPDATE SKIP LOCKED` does not provide real mutual
   exclusion in PostgreSQL 15.** Two concurrent claim attempts could both
   "win" and update the identical row. Fixed by never combining a CTE
   with the locking clause: fairness/eligibility ranking is now a plain
   *read-only* query producing a candidate shortlist; the actual claim is
   a plain, single-table `UPDATE ... WHERE id = (SELECT ... FOR UPDATE
   SKIP LOCKED)` — the same shape this codebase already used safely
   elsewhere for round/room claiming.
2. **The per-node capacity check is a check-then-act race.** Fixed by
   locking the node's own row (`SELECT ... FOR UPDATE`) before checking
   or claiming — this serializes concurrent attempts for *that one node*
   without affecting other nodes' throughput.

Both are covered by permanent regression tests in `test_sms_core.py`
(`test_concurrent_claims_never_double_claim_the_same_message`,
`test_concurrent_claims_never_exceed_node_capacity`) that use genuinely
separate pooled connections via `asyncio.gather` — a sequential call on
one connection, which every prior test in this file used, cannot reveal
either bug.

**Rule this codebase should not relitigate**: never select the target
row of a `FOR UPDATE SKIP LOCKED` claim through a CTE reference. If a
future routing decision needs a multi-step computation (ranking,
eligibility, scoring), compute it as a plain read first, then claim from
the resulting candidate list with a direct, single-table locking query.

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

## CSV bulk import (2026-09-10)

A CSV upload is genuinely just another way to create a campaign — no
second queue, no second message table, no second suppression check.
`packages/core/sms/csv_import.py` owns the whole pipeline; `campaigns.py`
only gained one branch (on `Campaign.import_job_id`) in
`validate_campaign()`/`start_campaign()` to resolve recipients from an
import job instead of an `audience_filter`.

```
POST /import/csv (multipart) -> parse -> normalize -> validate
    -> deduplicate -> suppression check -> persist sms_import_rows
    -> GET /import/{id} / /import/{id}/rows (preview + error table)
    -> POST /campaigns (import_job_id=...) -> POST /campaigns/{id}/validate
    -> POST /campaigns/{id}/start -> sms_messages, exactly like any
       other campaign, claimed by the existing pull protocol unchanged
```

Two supported shapes: `phone_number,message` (every row supplies its own
final text — no template/`body_override` needed; `sms_campaigns`' own
CHECK constraint was widened, migration `198d7fa10f43`, to accept
`import_job_id IS NOT NULL` as a third valid case) and `phone_number`
alone (sent through a template/`body_override` the campaign is created
with, exactly like an audience-filter campaign).

**Data model** — two new tables, both migration `198d7fa10f43`:
`sms_import_jobs` (one row per upload; `UNIQUE (tenant_id,
idempotency_key)` is the real double-submission defense — a retried
upload with the same key returns the already-computed summary rather
than re-parsing) and `sms_import_rows` (one row per parsed CSV row,
kept permanently as a real forensic record and the preview screen's own
paginated error table — the raw uploaded file itself is never persisted
anywhere, only its parsed, validated rows).

**Limits** (`packages/core/sms/csv_import.py`'s own constants, fixed
today, not yet admin-configurable): 10 MB max upload
(`MAX_CSV_SIZE_BYTES`), 50,000 max rows (`MAX_CSV_ROWS`), 500 rows per
DB batch (`IMPORT_BATCH_SIZE`, bounding both memory and any one
transaction's lock duration regardless of the file's total size), 10 SMS
segments max per `phone_message`-format row (`MAX_MESSAGE_SEGMENTS`). An
oversized upload is rejected in bounded 64 KB chunks
(`services/sms/app.py::_read_upload_bounded`) — never fully read into
memory before the size check fires.

**Phone normalization** moved to `packages/core/phone.py` (previously
duplicated via cross-service import from `services/bot/phone.py`) so the
CSV importer's free-typed numbers and the bot's Telegram-contact numbers
share the exact one implementation, per the directive's own explicit
"do not duplicate phone-normalization logic across multiple services."

**Security**: RBAC reuses existing permissions rather than inventing a
namespace — uploading/previewing a CSV needs `sms:campaigns:manage` (the
same permission creating any campaign draft needs), and the one route
that actually sends real messages, `POST /campaigns/{id}/start`, is
still gated by the narrower `sms:campaigns:approve` it always was. Both
new read routes (`GET /import/{id}`, `GET /import/{id}/rows`) are
tenant-scoped — a job id from a different tenant 404s, never returns
another tenant's row counts or content (see DECISIONS.md, 2026-09-10, for
the real gap this closes).

## RBAC

`sms:view` (broad, all four roles — matches `payments:view`'s own
breadth reasoning) plus five narrower manage permissions
(`sms:contacts:manage`, `sms:templates:manage`, `sms:campaigns:manage`,
`sms:nodes:manage`, `sms:compliance:manage`, all ops/superadmin) and one
deliberately superadmin-only gate, `sms:campaigns:approve` — the one
action in this whole product that actually sends real messages to real
people, mirroring `payments:approve`/`rooms:emergency_stop`'s own
least-privilege reasoning for the single highest-leverage lever. CSV
import (above) deliberately reuses these two exact permissions rather
than adding a third.

## Testing

- `tests/integration/test_sms_core.py` — 42 tests against real Postgres,
  no mocks: audience resolution, suppression, node lifecycle/health
  scoring, the full campaign state machine, message claiming/priority
  ordering, retry classification, the reconciliation sweep (a real
  backdated `assigned_at`, not a mocked clock), and Phase 2's node
  capacity/fleet-group eligibility/cross-campaign fairness/routing
  forensics, plus two explicit regression-protection tests (cross-tenant
  isolation, real audit-log content).
- `tests/integration/test_sms_app.py` — 13 tests over real HTTP: login
  against a real admin account, RBAC boundaries enforced through the
  actual dependency chain, a full campaign-to-delivery flow through the
  real node protocol, suppression enforcement, node-ownership rejection,
  and Phase 2's capacity/maintenance/fleet-group-eligibility routes.
- `tests/integration/test_sms_console_e2e.py` (`pytest -m e2e`) — a real
  Chromium tab driving the actual frontend: login, add a contact,
  register and approve a node, create/validate/start a campaign, and
  confirm the delivered result renders back in the UI.
- `tests/integration/test_sms_csv_import.py` — 31 tests: pure parsing/
  normalization/validation (no DB), idempotent/concurrent upload
  (`asyncio.gather`, two real pooled connections), the full import ->
  campaign -> validate -> start -> real `sms_messages` flow for both CSV
  shapes, suppression enforcement, double-start idempotency (sequential
  and genuinely concurrent), and the cross-tenant IDOR regression test.
- `tests/integration/test_sms_csv_import_app.py` — 9 tests over real
  HTTP: RBAC, a real 413 on an oversized upload (rejected in bounded
  chunks, not after a full read), a clean 422 on a malformed CSV,
  idempotent re-upload, and the full campaign-to-delivery flow including
  a real node claiming a CSV-created message.
- `tests/integration/test_sms_csv_import_console_e2e.py` (`pytest -m
  e2e`) — uploads a real file through a real `<input type="file">`,
  reads the validation summary and error table back from the DOM,
  creates a campaign from it, and drives it through the *existing*
  Campaigns screen with zero import-specific UI of its own from that
  point on.

Each test file that touches the message queue gives itself a fresh,
throwaway tenant (`test_sms_core.py`'s own `tenant_id` fixture) — the
same "polluted shared dev database" failure class already documented
elsewhere in `DECISIONS.md` would otherwise let two tests claim each
other's queued messages in this long-lived, never-truncated dev
database.

## What's deliberately not built yet

See `DECISIONS.md` (2026-09-07, both the Phase 1 and Phase 2 entries) for
the full list and reasoning: SMPP/carrier/HTTP provider adapters, fleet
scale validated beyond a handful of real nodes, a pluggable
multi-strategy routing engine (this slice's eligibility+fairness is real
production logic, not a stub, but it's one concrete policy, not an
interface with multiple implementations), backpressure/auto-throttling
beyond the capacity gate already built, automatic inbound STOP-keyword
suppression, a configurable N-person approval workflow, billing/usage
metering and per-tenant quotas, an outbound webhook/domain-event bus, the
real-time ops-center dashboard maturity (campaign digital twin, control
tower, capacity forecasting, anomaly/alert engines), load/chaos testing
at enterprise scale, node-protocol-version-gated rolling upgrades (the
version is stored and visible; nothing yet refuses to dispatch to an
incompatible version), and tenant self-service provisioning.

CSV import specifically (2026-09-10) does not yet include: background-
job/resumable-progress streaming beyond ~50,000 rows (the directive's
own "1,000,000+" tier — a real, tested, documented limit today, not a
scalability claim); SSE/WebSocket live campaign-progress updates (the
existing polling-refresh pattern every other SMS screen already uses);
a dedicated import-history console screen (fully auditable today via
`admin_audit_log`/`sms_import_rows`, just not its own UI yet); CSV
export of rejected rows; and admin-configurable per-tenant limits (the
constants above are fixed module-level values today).
