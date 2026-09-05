# Launch-Day Operations: Observability & Kill Switches

Two things an operator needs on launch day, in one place: what to watch,
and how to stop something immediately if it's going wrong. Every lever
below is real and already built — this document indexes and precisely
describes what exists, and is explicit about the one lever that's only
partial.

## What to watch

| Area | Metric / signal | Where |
|---|---|---|
| Application errors | Container logs, any `*_failed`/`*_exception` log line | `docker compose -f deploy/docker-compose.prod.yml logs -f <service>` |
| Request latency | `gateway_command_ack_seconds` (p50/p95/p99) | Grafana (`deploy/grafana/dashboards/jo-bingo.json`) / Prometheus |
| WebSocket connections | `gateway_connections` | Same |
| Active rooms | `engine_rooms_active` | Same |
| Bingo claims | `claim_attempts` table (valid/invalid split), `RoundVoided` alert | Direct query / Alertmanager |
| Deposits | `deposit_outcomes_total`, `DepositSuccessRateLow` alert | Grafana / Alertmanager |
| Withdrawals | `payout_queue_depth`, `PayoutQueueDepthHigh` alert | Same |
| Ledger integrity | `ledger_reconciliation_mismatch_count` (pushed), `ledger_reconciliation_sweep_mismatch_count` (scraped, new this pass), both alertable | Grafana / Alertmanager |
| Telebirr ingestion | `telebirr_ingestion_total` (by outcome), `TelebirrParserFailureSpike` alert | Same |
| Telebirr redemption | `telebirr_redemption_outcomes_total`, `TelebirrEvidenceReconciliationMismatch` alert | Same |
| Infrastructure (CPU/RAM/disk) | Host-level `docker stats` / node exporter if configured | Grafana, if a node-exporter dashboard is provisioned — not confirmed present; verify separately |
| Postgres/Redis health | `/healthz` on each service (process-alive only, not deep dependency health — see `docs/PRODUCTION_READINESS.md`'s own note on this scope limit) | `curl` each service's `/healthz` |
| Cloudflare Tunnel | `cloudflared tunnel info <name>` | Production host |

Correlating one specific event across these signals: every ledger
transaction has a real `id` and `idempotency_key`; every claim has a
`claim_attempts` row keyed by `(round_id, user_id, card_no)`; every
notification delivery has a `delivery_id`; every admin action has an
`admin_audit_log` row. Given any one of these, the others for the same
real-world event are reachable by a direct join — this wasn't built as a
single "trace ID" system, but the pieces needed to answer "what happened,
when, to whom" already exist and are already used this way in
`docs/INCIDENT_RESPONSE.md`'s own playbooks.

## Kill switches

| To stop | Lever | How immediate | Notes |
|---|---|---|---|
| New deposits (any rail) | `payment_provider_availability` toggle (admin console → Provider Availability, superadmin, `payments:configure`) | Immediate for new attempts | Doesn't affect in-flight Telebirr SMS ingestion, which keeps accumulating evidence safely even while redemption is off. |
| Telebirr specifically | Same toggle, `telebirr_sms` | Immediate | Currently OFF by default pending the real controlled test — see `docs/FINAL_HUMAN_ACTIONS.md` item 9. |
| Withdrawals | Same toggle mechanism, withdrawal rail | Immediate for new requests | Already-approved payouts already in flight through the worker are not retroactively stopped. |
| A specific Bingo room | `PATCH /rooms/{id}` with `{"is_active": false}` (`rooms:manage`) | **Partial, not immediate** — `RoundEngine` never reads `is_active` itself; only `EngineWorker.run_active_rooms()`'s *next room claim* consults it. Setting this stops the room from being *re-claimed* after its current engine naturally stops (crash, restart, or the round reaching a terminal state and the worker cycling) — it does **not** halt an in-flight round immediately. | **Real gap, not a launch blocker**: there is no single-room "stop this round right now" admin action today. To force an immediate stop: restart the `engine` container (stops every room's engine at once, not just one) after setting `is_active=false` for the target room so it doesn't get re-claimed on restart. Flagged here rather than silently assumed to be instant. |
| All Bingo activity | Restart the `engine` container | Immediate, but stops every room, not one | Every in-flight round's state is Postgres-durable — `recover_orphaned_rounds()` handles the restart cleanly, refunding rather than losing anything mid-round. |
| A specific bonus rule | Deactivate the rule (Bonuses & Referrals screen, `bonuses:manage_rules`) | Immediate for new grants | Existing grants already posted to the ledger are unaffected — correct, since they're already-settled real transactions. |
| Promotions broadly | No single global switch — deactivate each active rule individually | N/A | There is no "pause all promotions" button; this is a real, minor gap, not urgent given bonus rules are few and individually toggleable. |
| Notifications (one campaign) | Cancel the campaign (`notifications:send` or equivalent) | Immediate for `queued`/`scheduled` campaigns | Already-dispatched deliveries in flight are not recalled. |
| Notifications (everything) | No global kill switch by design — see `docs/PRODUCTION_ROLLBACK.md`'s own note | N/A | Reaching further requires a `bot` container rollback/stop, which also stops transactional messages (deposit confirmations, etc.) — a real trade-off, not an oversight. |
| A compromised admin account | Deactivate (Admin Users screen, superadmin, `admin_users:manage`) | **Immediate** — revokes the current session on the very next request, not just at next login (re-checked live, confirmed this session's own RBAC tests). | Reset the password on the same screen too — deactivation alone stops the session, not a credential someone else may have reused elsewhere. |
| A compromised payment agent | Deactivate (`services/admin/queries.py`'s agent deactivation, confirmed real: `UPDATE payment_agents SET is_active = false`) | Immediate for new submissions | |
| Any admin/agent session broadly | No single "revoke all sessions" button — deactivate accounts individually | N/A | For a suspected widespread compromise (not just one account), the more aggressive lever is rotating the Redis-backed session secret/flushing the session store — not a button in this codebase today; an infrastructure-level action. |

Every kill switch that goes through the admin console is automatically
audited (`admin_audit_log`, append-only, DB-trigger-enforced no
UPDATE/DELETE) — no emergency action taken this way is silent.

## The one real gap worth fixing before it's needed

**No immediate single-room stop.** If a specific room needs to halt
*right now* (a discovered exploit, a runaway bug affecting one room's
state), the only lever that's actually instant affects *every* room at
once (restarting the `engine` container). This is a real, scoped,
buildable improvement (an admin action that signals one `RoundEngine`'s
existing `stop()` cooperative mechanism directly, the same one
`EngineWorker.shutdown()` already uses per-engine) — flagged here as a
finding from this audit, not built during it, since this pass is in
launch-verification mode, not feature-building mode. If launch operators
consider this a real near-term risk, it's a small, well-scoped follow-up.
