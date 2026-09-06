# Business KPI Dictionary

Every KPI below names its real source table/column in the actual current
schema — not an aspirational one. Where a KPI needs data this schema
doesn't yet capture, that's stated explicitly as **NEEDS
INSTRUMENTATION**, not silently assumed available. This is a design
reference for `docs/BUSINESS_OPERATING_SYSTEM.md`, not running code —
see that document for what's actually being proposed to build, and when.

Financial KPIs use the real ledger/accounting model throughout
(`ledger_transactions`/`ledger_entries`/`accounts`/`account_balances`) —
never a parallel or approximated number.

## Room Liquidity (Section 22)

| KPI | Source |
|---|---|
| Current players | `count(DISTINCT user_id) FROM round_entries WHERE round_id = <the room's live round>` — exactly what `list_rooms()` already computes. |
| Average players (per round, over a window) | `AVG(player_count) FROM rounds` joined to a per-round entry count, grouped by `room_id`, filtered by `rounds.created_at` window. |
| Cards sold | `COUNT(*) FROM round_entries` per room/window (one row per card taken). |
| Stake volume | `SUM(rounds.pot) FROM rounds` per room/window — `pot` is already the real, settled total. |
| Payout volume | `SUM(round_winners.amount) FROM round_winners` joined to `rounds.room_id`, per window. |
| Round frequency | `COUNT(*) FROM rounds` per room/window, or derived from `rounds.seq` deltas over time. |
| Waiting time | `rounds.lobby_deadline - rounds.created_at` (or the actual lobby-to-running transition timestamp) — **NEEDS INSTRUMENTATION**: the exact moment a round left `lobby` for `running` is not currently persisted as its own timestamp column; derivable approximately from `claim_attempts`/`round_entries.joined_at` bounds, but a dedicated `rounds.started_at` column would make this exact. |
| Spectator count | Distinct WS connections viewing a room without a `round_entries` row — **NEEDS INSTRUMENTATION**: `gateway_connections` (Prometheus) counts total WS connections platform-wide, not per-room, and not split spectator-vs-player. Would need either a per-room connection counter or a room-scoped presence set in Redis. |
| Spectator conversion | Spectators who go on to take a card within N minutes — **NEEDS INSTRUMENTATION**, depends on spectator count existing first. |
| Repeat participation | `COUNT(DISTINCT round_id) FROM round_entries WHERE user_id = X AND room_id = Y` over a window — fully computable today. |
| Peak utilization | `MAX(player_count)` per room per hour-of-day bucket, from the same `round_entries` join as "current players." |
| Idle time | Time a room's engine holds the lock with 0 players between rounds — derivable from `rounds` timestamps once `started_at` (above) exists; approximable today from `rounds.created_at` gaps. |

**Classification** (HOT / HEALTHY / UNDERUTILIZED / LOW LIQUIDITY / RISK): a
rule applied to the above, e.g. (illustrative, not prescriptive —
real thresholds are a business decision, not an engineering one):
- HOT: average players ≥ room capacity × 0.8 sustained over the window.
- HEALTHY: rounds completing regularly with positive average player count.
- UNDERUTILIZED: rounds completing but average players well below
  `min_players`, relying on repeated underfilled-lobby refunds.
- LOW LIQUIDITY: rounds frequently voided for underfill.
- RISK: any room showing `RoundVoided` spikes disproportionate to its
  own history, or unusual repeat-pairing patterns (cross-reference with
  `shared_payout_account_clusters`/`repeat_room_pairings` from the
  existing Risk screen).

**Explicitly not automated**: this classification produces a label for a
human to look at, never an automatic game-rule change (Section 22's own
instruction).

## Promotion ROI (Section 23)

| KPI | Source |
|---|---|
| Eligible | `COUNT(*) FROM users WHERE <bonus_rules eligibility conditions>` at grant time — computable per rule, since `maybe_grant_referral_bonus`/`maybe_grant_welcome_bonus` already evaluate eligibility explicitly. |
| Reached | Users who saw the promotion — **NEEDS INSTRUMENTATION** for anything beyond a Notification Center campaign (which already has `notification_deliveries` with real per-recipient status); an organically-discovered bonus rule (e.g., a referral reward nobody was messaged about) has no "reached" concept distinct from "eligible" today. |
| Activated | `COUNT(*) FROM bonuses WHERE rule_id = X` — a real grant row exists the moment eligibility + trigger conditions are met. |
| Depositing | Join `bonuses` to `payments` on `user_id`, `status = 'succeeded'`, after `bonuses.created_at`. |
| Playing | Join to `round_entries` after `bonuses.created_at`. |
| Retained | Playing again N days after the bonus (see Player Lifecycle below). |
| Bonus granted | `SUM(bonuses.amount) WHERE rule_id = X` — real, already posted to the ledger (`bonus_grant` transactions). |
| Bonus converted | `SUM(bonuses.amount) WHERE rule_id = X AND status = 'converted'` — real, ledger-backed (`bonus_convert`). |
| Bonus expired | `SUM(bonuses.amount) WHERE rule_id = X AND status = 'expired'` — real. |
| Fraud blocked | Count of `maybe_grant_referral_bonus` calls that hit a fraud guard (self-referral, shared payout account) and returned without granting — **NEEDS INSTRUMENTATION**: these guards currently return silently (by design, to never fail the underlying deposit); a "blocked, and why" event isn't logged anywhere distinct today. Adding a log line or a counter at each guard's return point would close this cheaply, without changing the guard's own behavior. |
| Cost | `SUM(bonuses.amount) WHERE rule_id = X` (same as "bonus granted" — the real cost is the real grant total, not an estimate). |
| Net contribution | `(depositing users' real stake volume attributable to the rule) − (bonus cost) − (any fraud losses)` — the stake-volume side requires attributing `round_entries`/`rounds.pot` to users who received a specific bonus, which is a straightforward join but not a pre-built view today. |

**Cost per activated/retained user**: `cost / activated` and `cost /
retained` respectively — arithmetic over the above, not a new data need.

**Explicit anti-overclaim** (Section 23's own instruction): "depositing
after a bonus" is correlation, not proof the bonus *caused* the deposit —
this dictionary defines the measurable numbers; it does not claim they
establish causality, and no report built from it should either without
a real controlled comparison (e.g., an eligible-but-not-yet-granted
control group), which does not exist in this schema today.

## Referral Quality (Section 24)

| KPI | Source |
|---|---|
| Registrations | `COUNT(*) FROM users WHERE referred_by = X` — real, indexed (`ix_users_referred_by`). |
| Activations | Referred users who ever placed a stake — join to `round_entries`. |
| Depositing referrals | Join to `payments WHERE status = 'succeeded'`. |
| First-game referrals | `MIN(round_entries.joined_at) FROM round_entries WHERE user_id IN (referred users)`. |
| 30-day retained referrals | Referred users with a `round_entries` row both in their first week and 23-30 days later (see Player Lifecycle's "RETURNING" stage below for the shared definition). |
| Fraud-flagged referrals | Referrer/referee pairs surfaced by `referral_fraud_candidates_admin` (already built, on-demand) — becoming a real KPI just means persisting/counting its own output over time rather than only viewing it live. |
| Bonus cost | `SUM(bonuses.amount) WHERE trigger_type = 'referral_reward' AND referral_of_user_id IN (...)`. |
| Net contribution | Same shape as Promotion ROI's net contribution, scoped to referral-sourced users. |

**Ranking referrers by quality, not volume**: `net contribution / referrer`,
ranked descending — a referrer with fewer but real, depositing, retained
referees ranks above one with many registrations that never convert.

## Player Lifecycle (Section 26)

A **derived, read-only classification** — not a new `users.lifecycle_stage`
column requiring migration, at least initially (computing it live from
existing timestamps is simpler and always-correct; a cached column is an
optimization to add later only if query cost demands it).

| Stage | Definition (from existing columns) |
|---|---|
| NEW | `users.created_at` within the configured "new" window (e.g. 24h), no deposit yet. |
| ACTIVATED | Has a `round_entries` row (took a card) but no successful deposit yet — implies either a bonus-funded stake or (if the product ever allows it) a free/demo entry; today, every stake requires funds, so in practice ACTIVATED and FIRST-DEPOSIT are close together — this stage exists for schema completeness once/if that assumption changes. |
| FIRST-DEPOSIT | Exactly one `payments` row with `status = 'succeeded'`. |
| FIRST-GAME | Has both a successful deposit and a `round_entries` row. |
| RETURNING | A `round_entries` row in a period after their first, with a real gap (e.g., played in week 1 and week 2+). |
| REGULAR | Returning with a sustained cadence over a longer window (business-defined threshold — not invented here). |
| AT-RISK | Was REGULAR/RETURNING, now no `round_entries` row for a business-defined lapse window. |
| DORMANT | AT-RISK for a longer, business-defined period with still no activity. |
| RESTRICTED | `users.status IN ('limited', 'banned')` — already a real column value. |
| SELF-EXCLUDED | `users.status = 'self_excluded'` — already real. |
| FRAUD-REVIEW | Appears in `referral_fraud_candidates_admin`'s or `shared_payout_account_clusters`'s output and has not been cleared by a human reviewer — **NEEDS INSTRUMENTATION**: there's no `fraud_review_status` field today; these are on-demand queries, not a persisted review queue with a decision recorded. Building a real review-queue table (candidate → reviewed-by → decision → timestamp) is exactly what Section 25's Risk Command Center below would need anyway. |

**Explicit guardrail** (Section 26's own instruction): this classification
feeds analytics and *appropriate* communication (e.g., a RETURNING
player might get a session-time reminder tuned differently than a NEW
one) — it must never drive aggressive loss-chasing incentives (e.g., a
"come back, here's a bonus" message timed to an AT-RISK player who just
had a losing session). Any message content built on this lifecycle data
goes through the same Notification Center audience-exclusion rules
already enforced (self-excluded/banned/cooling-off unconditionally
excluded) — this dictionary does not propose loosening that.

## Trust Center (Section 27)

Every item is already a real, queryable row — this section is a
read-only *player-facing view* of data the platform already has, not new
data:

| Player-visible item | Source |
|---|---|
| Wallet history | `ledger_entries` for the player's own accounts, joined to `ledger_transactions` for kind/memo. |
| Deposits | `payments WHERE user_id = X AND kind = 'deposit'`. |
| Withdrawals | `payments WHERE user_id = X AND kind = 'withdrawal'`. |
| Games | `round_entries WHERE user_id = X`, joined to `rounds` for outcome. |
| Cards | Same join, `card_no` + `round_id`. |
| Wins | `round_winners WHERE user_id = X`. |
| Bonuses | `bonuses WHERE user_id = X`. |
| References | `payments.provider_ref`/`payment_evidence.reference` for the relevant row. |

Each answers what/when/amount/reference/status directly from existing
columns — no new backend computation, only a new (player-facing, i.e.
Mini App) presentation of data the admin console already shows an
operator.

## Daily CEO Brief (Section 28) — composition, not new data

Every line item in the brief template is a roll-up of the KPIs already
defined above, plus the platform's own existing operational signals
(reconciliation status, `RoundVoided` rate, responsible-gaming indicator
counts by status). "System incidents" pulls from `docs/INCIDENT_RESPONSE.md`'s
own real severity levels, if incidents are logged anywhere structured
(today: they are not — **NEEDS INSTRUMENTATION**, a real incident log is
itself a small, worthwhile addition independent of this business layer).

## Business Alerts (Section 29) — thresholds, not new metrics

Every named alert condition is a threshold over a KPI already defined
above (e.g., "participation collapse" = a room-liquidity KPI dropping
sharply from its own trailing baseline; "withdrawal backlog" =
`payout_queue_depth`, already a real scraped metric; "reconciliation
mismatch" = `ledger_reconciliation_sweep_mismatch_count`, already real
and alertable as of this pass). New work here is *threshold tuning and
routing*, not new data collection, for every item except "referral fraud
increase" (needs the fraud-candidate counts to be persisted over time,
not just computed on demand) and "bonus liability spike" (needs a
time-series of `SUM(bonuses.amount) WHERE status='active'`, computable
from existing data but not currently tracked as a trend).
