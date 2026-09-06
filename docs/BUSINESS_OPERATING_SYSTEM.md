# Business Operating System — Design

This is a **design document**, not an implementation record. Nothing in
Sections 21-32 of the directive this responds to has been built as
running code this pass — building nine parallel BI subsystems (Room
Liquidity, Promotion ROI, Referral Quality, Risk Command Center, Finance
Command Center, Player Lifecycle, Retention Analytics, a Daily CEO Brief,
Business Alerts) in one pass, on top of an already-large launch-readiness
engagement completed today, would be the reckless move, not the careful
one — real financial-adjacent reporting deserves the same test discipline
everything else in this codebase gets, not a rushed first draft. This
document exists so that work can start from a real, schema-grounded plan
instead of a blank page.

`docs/BUSINESS_KPI_DICTIONARY.md` is the companion piece: every KPI named
below is defined there with its real source table/column, or flagged
explicitly as needing new instrumentation.

## What this is and isn't

**DATA → INSIGHT → DECISION → ACTION → MEASUREMENT** (Section 21's own
framing) describes an intelligence *layer* on top of the existing
platform — read-mostly, reporting-first. It is explicitly **not**:
- A new set of money-movement code paths (every financial number it
  shows is read from the existing ledger, never computed independently
  of it — see the KPI dictionary's own "financial KPIs use the real
  ledger model" rule).
- A replacement for the existing admin console's operational screens
  (Payments, Bonuses, Risk, Notifications) — those remain the tools an
  operator *acts* through; this layer is where they *notice* what needs
  acting on.
- An automatic decision-maker. Section 32's own AI-assisted-operations
  boundary applies to the whole system, not just AI: nothing in this
  design proposes an automated system that mutates balances, approves
  withdrawals, confiscates funds, bans users, overrides responsible-
  gaming controls, activates high-risk promotions, or changes payout
  rules on its own. Every KPI and alert here ends at a human decision
  point.

## Proposed architecture (consistent with this codebase's existing shape)

- **No new services.** Every KPI is a read query against the existing
  Postgres database — the same pattern `services/admin/queries.py`
  already uses for the current admin console's own reporting screens
  (`reports:view`, `risk:view`). A "Business Control Center" is a new
  set of admin routes/screens in the *existing* `services/admin` app,
  gated by the *existing* RBAC table (see `docs/RBAC_MATRIX.md`) — not a
  new deployable.
- **New permissions, not new roles.** Following the exact precedent
  `bonuses:view_fraud_signals`/`risk:view` already set: a `business:view`
  permission (breadth TBD by the business — likely finance+ops+superadmin,
  mirroring `risk:view`'s own audience) for the reporting views, and a
  narrower one for anything that writes (e.g., a fraud-review decision —
  see Risk Command Center below) mirroring `payments:approve`'s shape.
- **The Daily CEO Brief is a scheduled report, using the pattern
  already established for every other periodic job in this codebase**:
  a function callable both on-demand (for an admin screen) and from
  `payout_worker.py`'s existing `_run_periodic_sweep()` mechanism (e.g.,
  compose it once daily and post it somewhere — Telegram DM to a
  configured admin, or simply make it available at a fixed admin-console
  URL). No new scheduler, matching this pass's own ledger-reconciliation
  precedent.
- **Business Alerts reuse the existing Prometheus/Alertmanager stack.**
  Each named alert in the KPI dictionary is either already a real gauge
  (`payout_queue_depth`, `ledger_reconciliation_sweep_mismatch_count`) or
  a new gauge of the identical shape, computed by a periodic sweep the
  same way. Not a second alerting system.

## Risk Command Center (Section 25) — the one subsystem needing new durable state

Every other subsystem in this design is purely additive reporting over
existing tables. The Risk Command Center is the one exception worth
calling out precisely: **DETECT → REVIEW → DECIDE → AUDIT** as a real
workflow needs a persisted review queue, because "detect" alone
(`shared_payout_account_clusters`, `repeat_room_pairings`,
`referral_fraud_candidates_admin`) already exists as real, on-demand,
human-review-first queries — but there is nowhere today to record that a
human *looked at* a candidate and *decided* something about it. A
minimal, schema-consistent addition:

```sql
CREATE TABLE fraud_review_queue (
  id                bigserial PRIMARY KEY,
  candidate_kind    text NOT NULL,   -- 'shared_payout_account', 'repeat_room_pairing', 'referral_burst', ...
  candidate_key     text NOT NULL,   -- e.g. the user-id pair or cluster id the detector produced
  detected_at       timestamptz NOT NULL DEFAULT now(),
  status            text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'reviewing', 'cleared', 'confirmed_action_taken')),
  reviewed_by_admin_id bigint REFERENCES admin_users(id),
  decision_reason   text,
  decided_at        timestamptz
);
```

This is a small, powerful primitive — one table, one lifecycle — not a
new fraud-detection engine. The existing detectors keep doing exactly
what they do today (surface candidates); this just gives a human's
decision about a candidate somewhere durable to live, closing the
audit loop Section 25 asks for. **Heuristics still never auto-confiscate
money** — this table only ever records that a human looked and decided;
nothing reads it to trigger an automatic financial action.

## Business Health (Section 31) — composed, not a black-box score

Seven named areas (Game Liquidity, Payment Health, Player Retention,
Promotion Efficiency, Fraud Risk, Responsible Gaming, System Reliability)
each already have concrete, named KPIs in the dictionary above (or in
this codebase's existing metrics for System Reliability specifically —
`docs/PRODUCTION_READINESS.md`'s own evidence). "Every score must be
drillable" (Section 31's own instruction) means each area's score is
never a single opaque number — it's a small set of the named KPIs above,
shown together, with the score itself defined as a simple, stated
function of them (e.g., a weighted average with published weights), so
anyone looking at "Payment Health: 82" can click through to exactly which
component numbers produced 82 and why. No proposal here to build a
machine-learned or otherwise unexplained "AI score."

## AI-assisted operations (Section 32) — the explicit boundary

If/when an AI-assisted layer is added on top of this reporting system
(summarizing trends, flagging anomalies, drafting a promotion
suggestion), the boundary is structural, not a prompt instruction to
"please don't do X":
- AI-facing endpoints in this design are **read-only** against the KPIs
  above — there is no code path for an AI-driven recommendation to
  directly call `payments:approve`, `users:adjust_balance`,
  `bonuses:grant`, `admin_users:manage`, or any responsible-gaming
  control. A recommendation is text or a suggested draft; a human with
  the real RBAC permission takes the real action through the real,
  existing, audited admin console flow — the same way a human typing a
  suggestion into a form today has to click "Approve" themselves.
- Any AI-drafted artifact (a suggested promotion, a suggested campaign)
  lands as a **draft** in the existing systems' own draft states
  (`notification_campaigns.status = 'draft'`, a bonus rule with
  `is_active = false`) — never auto-published, auto-activated, or
  auto-sent. This is enforced by the *existing* code (drafts require an
  explicit, separately-permissioned send/activate action already), not a
  new guardrail this design has to invent.

## Suggested phasing (a recommendation, not a commitment made here)

Given "small powerful primitives, not feature bloat" and this system's
own real financial stakes, a sensible build order — cheapest and most
independently valuable first:

1. **Trust Center** (Section 27) — pure read-only surfacing of data
   that already exists; zero new schema; highest player-trust value per
   unit of effort.
2. **Business Alerts** (Section 29) — mostly threshold-tuning over
   metrics that already exist; the two "NEEDS INSTRUMENTATION" items
   (fraud-candidate trend, bonus-liability trend) are small, additive
   gauges following this pass's own `ledger_reconciliation_sweep`
   pattern exactly.
3. **Room Liquidity + Referral Quality + Promotion ROI reporting
   screens** — all pure read queries per the dictionary above; the
   "NEEDS INSTRUMENTATION" items (spectator count, `rounds.started_at`,
   fraud-block counters) are each small, independent additions that
   don't block shipping the rest of each screen first with the data
   that's already available.
4. **Risk Command Center's review queue** — the one new table in this
   whole design; unlocks turning already-existing detection queries into
   a real, auditable workflow.
5. **Player Lifecycle classification + Daily CEO Brief** — composition
   of everything above; naturally sequenced last since it's most useful
   once the component KPIs it rolls up already exist and are trusted.
6. **Business Health scores + any AI-assisted summarization** — last,
   deliberately, since these are the most visible/judged surfaces and
   should sit on top of KPIs that have already been shipped, verified
   against real data, and trusted by whoever's using them daily.

Each phase should ship with the same discipline as every other feature
in this codebase's history: real tests against real Postgres, an honest
PASS/FAIL evidence record, no speculative abstraction ahead of what's
actually being measured yet.
