# Incident Response

This document didn't exist before this pass — a real, confirmed gap for
a real-money platform (`grep`-confirmed: no equivalent file, no section
of another doc covering this, anywhere in the repo). What follows is
built from the real tools and controls this codebase actually has —
every command below is a real endpoint/flag/script that exists today,
not aspirational tooling.

## Severity levels

| Level | Definition | Example | Response |
|---|---|---|---|
| SEV1 | Real money at risk, or the platform is down for all players | Ledger imbalance, deposits/withdrawals failing platform-wide, the gateway is unreachable | Immediate, all-hands, follow this doc's SEV1 playbooks below |
| SEV2 | A real feature is broken but money/core play is safe | Notifications not sending, Bot Content not updating, admin console down but the game itself is fine | Same-day fix, no emergency rollback needed unless it escalates |
| SEV3 | Degraded but working | A slow query, an elevated (not zero) delivery failure rate | Normal work queue |

## First 5 minutes, any SEV1

1. Check `/healthz` on every service (`arada.fun`, `payments.arada.fun`,
   `admin.arada.fun`) — confirms which processes are actually up.
2. Check Grafana (`deploy/grafana/dashboards/jo-bingo.json`) for the
   metric that matches the symptom (see the table below).
3. Check `deploy/prometheus/alerts.yml`'s 8 rules — is one already
   firing? (`LedgerReconciliationMismatch`, `PaymentReconciliationMismatch`,
   `PayoutQueueDepthHigh`, `DepositSuccessRateLow`,
   `CallAckLatencyP99High`, `RoundVoided`,
   `TelebirrEvidenceReconciliationMismatch`,
   `TelebirrParserFailureSpike`).
4. Pull real logs: `docker compose -f deploy/docker-compose.prod.yml logs
   --tail=200 <service>` for whichever service the symptom points at.
5. **Do not guess and "fix" blind.** Every playbook below starts with a
   real query to confirm the actual state before touching anything.

## Playbook: suspected ledger imbalance

1. Confirm, don't assume: run `packages/core/ledger.py::reconcile()`
   (via `python -m packages.core.reconcile_job`, or the equivalent query
   directly) — it returns every `(account_id, cached_balance,
   computed_balance)` mismatch, empty means genuinely reconciled.
2. If real mismatches exist: **do not manually edit `account_balances`.**
   Every legitimate balance change in this codebase goes through
   `ledger.post()` — a mismatch means the *cache* has drifted from the
   *entries* (the entries are the source of truth), so the fix is a
   corrective `ledger.post()` entry, never a raw `UPDATE`.
3. Identify which subsystem posted the divergent transaction via
   `ledger_transactions.created_by`/`memo` and `admin_audit_log` for
   anything admin-initiated around the same timestamp.
4. This is exactly the class of incident `docs/PRODUCTION_READINESS.md`
   flags disaster-recovery restore for if the drift is severe enough —
   don't reach for a database restore before confirming a targeted
   ledger correction can't fix it with far less blast radius.

## Playbook: deposits/withdrawals failing platform-wide

1. Check `payment_provider_availability` (admin console → Provider
   Availability, or a direct query) — is the relevant rail (`chapa`/
   `manual`/`telebirr_sms`) actually enabled? A rail correctly disabled
   is not an incident.
2. Check `payout_worker`'s own health — it's one process running the
   payout stream consumer *and* 5 periodic sweeps (deposit poll,
   withdrawal sweep, 2 reconciliations, bonus sweep). If the container is
   down, **all 6 of those stop at once** — a single point of failure
   worth knowing about before assuming 6 separate bugs.
3. Check the `payments` service's own `/healthz` and its Chapa API
   connectivity specifically (a Chapa-side outage looks identical to a
   local bug from the metrics alone).
4. **Rollback lever, already built for exactly this**: flip
   `payment_provider_availability` for the affected rail to disabled via
   the admin console (superadmin, `payments:configure`) — stops new
   attempts through that rail instantly without touching Bingo, the
   ledger, or any other rail. This is the correct first move for a
   provider-specific incident, before considering a code rollback.

## Playbook: a bot/notification/campaign incident

1. `campaign_worker_tick_failed` or `bonus_sweep`-related log lines in
   the `bot`/`payments` container logs — a bad tick logs and continues,
   it doesn't crash the loop; if ticks have stopped entirely, the
   background task itself likely died (check the container is on the
   current build, not a stale image predating a given feature).
2. A stuck-`processing` notification delivery self-heals within 15
   minutes (`RECLAIM_STUCK_AFTER_SECONDS`,
   `services/bot/campaign_worker.py`) — this is not an incident on its
   own unless `notification_delivery_reclaimed_from_stuck_processing` is
   firing at a *sustained* rate (that points at the `bot` container
   crash-looping, a real incident; an occasional one is expected
   background noise).
3. **Never manually re-send a campaign to "fix" a delivery problem**
   without first confirming via `GET /notifications/campaigns/{id}/
   deliveries` which specific rows actually failed and why — a blind
   resend risks a real duplicate broadcast to players who already got it.

## Playbook: a suspected security incident (compromised admin account)

1. Immediate: deactivate the account (**Admin Users** screen,
   superadmin, `admin_users:manage`) — this revokes the account's
   *current* session immediately (re-checked on every request, not just
   at login), not merely at next-login.
2. Reset the password on the same screen regardless — deactivation alone
   stops the *session*, not a credential someone else may have reused
   elsewhere.
3. Pull `GET /audit-log?admin_id=<id>` — every mutation that admin made
   is here, with before/after values, immutable (Postgres itself refuses
   UPDATE/DELETE on this table).
4. For anything the compromised account touched that moved money
   (`payments:approve`, `bonuses:grant`, `payments:configure`): treat
   each audited action as a real transaction needing individual review,
   not a blanket rollback — the audit log's before/after values are
   exactly what's needed to evaluate each one on its own.
5. If the IP allowlist (`ADMIN_IP_ALLOWLIST`) isn't already restricting
   admin access to known networks, this is the moment to reconsider
   whether it should be.

## Escalation

This document does not have a real paging/on-call contact list — that's
a genuine operational detail only the team running this platform can
fill in, not something to fabricate here. Fill in:

- Who gets paged for a SEV1, and how (confirm Alertmanager's receiver is
  actually wired to reach them — flagged `UNVERIFIED` in `docs/
  PRODUCTION_READINESS.md`, since a firing alert with no real receiver
  behind it pages no one).
- Who has production SSH access and can run the playbooks above for real.
- Who has legal/compliance authority to decide on player-facing
  communication during an incident (a self-exclusion or KYC-related
  incident may have regulatory reporting obligations — outside this
  document's scope to judge).
