# Production Smoke Test

A real operator walks these 13 steps against the real, deployed
production system after a deploy (or before declaring launch-ready).
Each step names the expected result and the evidence to capture — this
is a checklist to execute for real, not a description of what should
work in theory.

| # | Step | Expected result | Evidence to capture |
|---|---|---|---|
| 1 | Open the real Mini App from the real Telegram bot | Mini App loads, no error screen | Screenshot |
| 2 | Authenticate (automatic via Telegram) | Wallet/lobby screen shows the real logged-in user's name | Screenshot |
| 3 | View wallet | Real cash/bonus/locked balances shown, matching a direct `SELECT` against `account_balances` for that user | Screenshot + the SQL query result |
| 4 | View the Bingo room list | At least one real room shown with a live player count and status | Screenshot |
| 5 | Enter a room with 0 current players | Room shows `status: lobby`, `players: 0`, and a live countdown — doesn't error or vanish | Screenshot (this exact behavior was verified this pass — see `docs/LAUNCH_BLOCKERS.md` LB-A2 — this step confirms it holds in real production too) |
| 6 | Purchase/select a card | Real stake debited from cash balance, card shown as held | Screenshot + wallet balance before/after |
| 7 | Play through a round | Numbers call in real time, card marks update | Screen recording |
| 8 | Trigger a controlled winning state | A genuine two-line win is recognized and a `claim_result` with `ok: true` is received | Screen recording + the `claim_attempts` row for this claim |
| 9 | Verify payout | Real cash balance increases by the correct payout amount | Wallet balance before/after + the resulting `ledger_transactions`/`ledger_entries` rows |
| 10 | Verify ledger | `packages/core/ledger.py::reconcile()` returns zero mismatches for the affected accounts immediately after | The reconcile query output |
| 11 | Test a real notification | Trigger any real transactional notification (e.g. a deposit confirmation) and confirm it arrives in Telegram | Screenshot of the received message |
| 12 | Test admin console access | A real admin logs in (password + TOTP), sees the round/transaction just created in the relevant screen | Screenshot |
| 13 | Verify monitoring reflects this session | Grafana/Prometheus shows the real activity just generated (e.g. `engine_calls_total`, `ledger_transactions_total` incrementing) | Screenshot of the dashboard/metric |

## Pass criteria

Every step completes with its expected result and evidence captured. No
step should require guessing whether it worked — each has a concrete,
checkable outcome.

## Fail criteria and what to do

Any step failing is a real, current production issue, not a theoretical
one. Match it to the relevant `docs/INCIDENT_RESPONSE.md` playbook by
symptom (ledger imbalance, payment failure, notification failure,
security concern) before taking any corrective action — don't guess-fix.

## Relationship to other documents

This is the fully-integrated, real-money end-to-end walk. It assumes:
- `docs/LAUNCH_BLOCKERS.md`'s Category A items are already done (they
  are, as of this pass).
- `docs/FINAL_HUMAN_ACTIONS.md`'s items 1-6 (HTTPS, backups, restore
  drill, production-code confirmation, alerting, hostname re-check) are
  ideally done first — this smoke test exercises the *application*, not
  the infrastructure those items verify independently.
- Step 8-9 constitute (or can double as) the "controlled real-money
  test" `docs/FINAL_HUMAN_ACTIONS.md` item 8 already calls for — run
  them together rather than as two separate real-money exercises if the
  business prefers minimizing real transactions during testing.
