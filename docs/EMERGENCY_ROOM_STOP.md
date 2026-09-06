# Emergency Room Stop

An operator's ability to immediately halt one specific, money-bearing
Bingo room and safely refund it — closing a real, confirmed gap: the
existing `RoundEngine.stop()` cooperative mechanism only ever prevented
the *next* round from starting; it never reached an *active* round's own
number-calling loop at all (see `DECISIONS.md`'s entry on the finding
that motivated this feature).

## 1. Complete lifecycle audit

Traced directly from `services/engine/round_engine.py`, not assumed:

```
IDLE --first round created--> LOBBY --countdown ends, enough players--> RUNNING
                                 |
                                 +--not enough players--> refund, void --> IDLE

RUNNING --valid claim (+ 50ms tie window)--> SETTLING --> DONE --> IDLE
RUNNING --75 calls, no winner--> refund, void --> IDLE
```

**A critical, previously-undocumented fact this audit confirmed**:
`self._status` (`idle`/`lobby`/`running`/`settling`) is a **pure
in-memory Python attribute** — `_set_status()` never writes it to
Postgres. The *database* `rounds.status` column only ever changes at
four explicit write sites: `_start_new_round()` (→ `lobby`),
`_transition_to_running()` (→ `running`), `_settle_with_winners()` (→
`done`), and `refunds.py::refund_round_in_transaction()` (→ `voided`).
**"Settling" never appears in the database at all** — it's purely an
engine-local bookkeeping state during the 50ms tie window. Any code that
needs to reason about a round durably (across process restarts, or from
a *different* process like the admin console) must think in terms of the
four real database values, not the five in-memory ones.

**What happens today if something external changes `rounds.status` while
an engine is still actively running that round** (this is exactly what
an emergency stop does, and — this audit discovered — exactly what the
*already-shipped* `POST /rounds/{id}/void` action could always do too,
completely unguarded, before this pass):

| Round's in-memory state | What the live engine does, before this fix | Real risk |
|---|---|---|
| `idle` / `lobby` (not yet running) | `_run_lobby()` already checks `_stop_requested`/lock every 1s tick and returns early | None — no real money at stake yet if 0 joined, and an existing join's stake is safe in `pot_escrow`, refundable by whoever changed the DB row |
| `running` | `_run_running()`'s loop checked only `self._status` (never re-read the DB) and `self._lock.is_held()` — it had **no way to notice** an external status change and kept calling numbers into a room that was already voided in the database | Confusing UI for players (numbers keep calling in a "stopped" room); not a financial risk in itself, since nothing pays out just from calling a number |
| `settling` (in-memory only; DB still says `running`) | `_settle_with_winners()` credited the determined winner(s) with **no check at all** that the round hadn't already been voided out from under it by a concurrent admin action | **Real risk, confirmed by this audit, not hypothetical**: a round already refunded by an admin's void action could *also* be settled and paid out by the engine moments later — a genuine double-payment (refunded AND paid), because neither code path checked what the other had already done |

## 2. Safe stop contract

Existing architecture already expresses everything needed — **no new
round-status values were added**, matching the explicit instruction not
to invent unnecessary states:

- **NORMAL** = `rounds.status IN ('lobby', 'running')`, no stop
  requested.
- **STOPPING** = the moment an admin's transaction acquires the row lock
  (`FOR UPDATE`) inside `refund_round_in_transaction()` — a real,
  Postgres-enforced critical section other writers (the engine's own
  settlement) block against.
- **STOPPED** = `rounds.status = 'voided'` (the same terminal value
  every other refund path already produces — underfilled lobby,
  exhausted-no-winner, or now an admin's emergency stop, distinguished
  only by the ledger memo's reason text, e.g. `admin_emergency_stop:
  <reason>`).
- **SETTLED** = `rounds.status = 'done'` (unchanged, pre-existing).

`STOP_REQUESTED` was deliberately *not* added as a separate durable
state: the admin action either succeeds in acquiring the row lock and
transitioning straight to `voided`/`STOPPED`, or the round is already
terminal and the action is a documented no-op. There is no intermediate
window where a durable "someone asked to stop this" flag needs to
persist on its own — the row lock itself *is* the synchronization
primitive, resolved within one transaction, not left pending.

## 3. Active-round stop behavior — exact sequence implemented

```
operator → STOP ROOM (admin console, superadmin-only)
  → reason required (services/admin/app.py::_require_reason(), the same
    helper every other consequential admin action already uses)
  → literal "STOP" confirmation required (queries.py::
    STOP_ROOM_CONFIRMATION_TOKEN, same shape as responsible_gaming
    .SELF_EXCLUDE_CONFIRMATION_TOKEN)
  → authorization verified (rooms:emergency_stop, superadmin-only RBAC)
  → one transaction: SELECT ... FOR UPDATE locks the room's current round
  → refund_round_in_transaction() -- the existing, already-tested
    financial primitive; posts one real `refund` ledger transaction per
    entrant, sets rounds.status = 'voided'
  → rooms.is_active = false, in the SAME transaction
  → audit.record(), in the SAME transaction (atomic with the refund --
    the same fix void_round_admin() already made for its own action:
    a crash between refund and audit-write is structurally impossible,
    not just unlikely)
  → transaction commits
  → live engine notices on its very next number-call attempt
    (_call_next_number()'s own UPDATE ... WHERE status = 'running'
    RETURNING id -- matches zero rows once voided, which it now treats
    as "stop calling, reset to idle") -- typically within one
    call_interval_ms tick, not up to 30 seconds
  → players get a real balance-update push (ledger.publish_balance_
    update) AND a persistent Telegram notification
    (notify.room_emergency_stopped, both en/am locales) AND a live room
    WS event (round_voided, reason=admin_emergency_stop) for anyone
    still actively watching
```

Explicitly **never**: a direct balance edit, round-row deletion,
player-entry deletion, process kill, or any code path that bypasses
`ledger.post()`.

## 4. Concurrency safety — what's actually guaranteed, and why

The core guarantee: **`refund_round_in_transaction()`'s own `SELECT ...
FOR UPDATE`** (pre-existing) **and `_settle_with_winners()`'s new,
identical `FOR UPDATE` + terminal-status check** (added this pass) make
"an admin stops this round" and "the engine settles this round" mutually
exclusive at the Postgres row-lock level — whichever transaction reaches
the row first commits the round's one real terminal outcome; the other
sees that committed state and backs off. This is enforced by the
database, not by hoping the timing works out.

| Scenario | Proven by | Outcome |
|---|---|---|
| Stop + a number call at the same instant | `test_stop_running_room_refunds_and_halts_number_calling` | Refund completes; the engine stops calling further numbers within one interval tick |
| Stop + a player claims at the same instant | `test_admin_stop_racing_real_settlement_produces_exactly_one_outcome` (10x repeated in isolation, clean every time) | Exactly one outcome: either the claim's settlement wins the row lock and pays out (stop finds it already terminal, refunds nothing), or the stop wins and refunds both players (settlement finds it already terminal, pays nothing) — never both |
| Stop + settlement starts at the same instant | Same test as above | Same guarantee — this *is* the settlement-start race |
| Stop + engine crash at the same instant | Not separately tested this pass (existing crash-recovery coverage — `recover_orphaned_rounds()` — already handles "engine died mid-round" unconditionally; a stop that already committed leaves the round `voided`, a terminal status recovery's own `NON_TERMINAL_STATUSES` check correctly ignores) | Covered by existing, tested crash-recovery machinery, not new code |
| Two operators stop the same room concurrently | `test_concurrent_stop_by_two_admins_refunds_exactly_once` (5x repeated, clean every time) | Exactly one refund (`[0, 2]` entrant split every run); zero double-refunds |
| Stop requested twice (same or different admin) | `test_duplicate_stop_is_idempotent` | Second call refunds 0 — real, DB-enforced idempotency, not an application-level guess |

**Required invariants, checked directly in these tests, not assumed**:
exactly one authoritative terminal outcome (✅, every race test asserts
this explicitly by branching on the real resulting `rounds.status`); no
double refund (✅, DB row lock); no payout after a stop that already
committed (✅, the new settlement guard); no lost stake (✅, every test
verifies the exact expected balance, not just "some balance"); no
duplicate notification (the notification is sent exactly once, from
inside `stop_room_admin()` itself, only on the branch that actually
performed a real refund — a no-op call sends nothing); no inconsistent
room state (✅, `_reset_to_idle()` is called on every path that detects
an external stop, confirmed by `test_stop_running_room_...` continuing
to poll `call_index` for a full second afterward and finding it frozen,
not corrupted or still incrementing).

## 5. Durability

No new column, no new table. The design deliberately reuses `rounds
.status` (already durable, already the real source of truth every other
part of this codebase already treats it as) and `rooms.is_active`
(already durable, already the mechanism `run_active_rooms()` already
checks before re-claiming a room). Because these are ordinary Postgres
columns:

- **Engine restart**: `recover_orphaned_rounds()` already treats any
  non-terminal round with no live lock-holder as orphaned and refunds
  it — a round already `voided` by a stop is simply already terminal,
  so recovery correctly does nothing further to it.
- **Worker restart**: `run_active_rooms()` already reads `rooms
  .is_active` before claiming — a stopped room (`is_active = false`)
  is never re-claimed, full stop, regardless of how many times the
  worker process restarts.
- **Redis restart**: the stop action itself never touches Redis for its
  *decision* (only for the post-commit notification/broadcast, which is
  best-effort UX, not the financial guarantee) — a Redis outage during
  a stop would leave the refund fully committed in Postgres; only the
  live-notification push would be delayed, exactly the same class of
  degradation `docs/DISASTER_RECOVERY_DRILL.md` scenario 3 (Redis loss)
  already documents as safe-by-design for this codebase.
- **Database reconnects**: the stop action's own transaction either
  commits fully or not at all (standard Postgres transaction semantics)
  — there is no partial-commit state to reconcile.

"The room must not accidentally resume a round that an operator
explicitly stopped" — verified structurally: a stopped round's status
(`voided`) is `TERMINAL_STATUSES`-terminal to every piece of code that
checks it (recovery, settlement's new guard, `refund_round_in_
transaction()` itself), and the room's own `is_active = false` prevents
any engine from ever claiming it again in the first place.

## 6. Admin UI

`web/admin/js/screens/rooms.js`'s new "Stop room" button (styled
`.btn-danger`, never a bare ambiguous "Stop") opens a real confirmation
panel showing the **actual current** room code, round id, round status,
player count, and staked amount (fetched live from `GET /rooms/{id}
/stop-preview` — never a cached or guessed number), a plain-language
statement of the real financial consequence, a required reason field,
and a required literal "STOP" confirmation field — submitting with
either missing produces a client-side rejection before any request is
even sent; the server independently re-validates both regardless (never
trusting client-side validation alone for a money-moving action).
Verified end to end in a real Chromium tab
(`test_superadmin_stops_a_running_room_over_a_real_browser`, 3x
repeated, clean every time): the live preview shows the real "running"
state and real staked amount, an incomplete submission does not proceed,
and a correct one closes the panel and leaves both players' real ledger
balances exactly refunded.

## 7. Player experience

`notify.room_emergency_stopped` (added to `en.json` and `am.json`,
matching this codebase's own `test_am_and_en_have_matching_key_sets`
requirement; `om`/`ti` fall back to English per this codebase's existing,
tested fallback behavior): *"This game was stopped by the operator for
safety/technical reasons. Your stake of {amount} ETB has been refunded
to your wallet. Reference: {reference}"* — sent only **after** the
refund transaction has actually committed, never before, per the
explicit "never promise a refund before the ledger confirms it" rule.
Paired with a live room WS broadcast (`round_voided`, `reason:
"admin_emergency_stop"`) for anyone still actively watching the room
screen at that moment, and a real balance-update push so a connected
Mini App's on-screen balance updates immediately rather than waiting for
a manual refresh.

## 8. Audit

Every stop action writes one `admin_audit_log` row
(`action: "rooms.emergency_stop"`, `target_type: "room"`) inside the
same transaction as the refund itself — `admin_id`, `room_id` (as
`target_id`), `reason`, real `before`/`after` snapshots (room's prior
`is_active` value, the round's prior status, the refunded-entrant
count), and `ip_address`. Immutable under this codebase's existing audit
architecture (`admin_audit_log` has no `UPDATE`/`DELETE` grant at the
database level — confirmed, unchanged, pre-existing).

## 9. The 16 critical stop scenarios

| # | Scenario | Status | Test |
|---|---|---|---|
| 1 | Stop idle room | ✅ Tested | `test_stop_idle_room_just_deactivates_it` |
| 2 | Stop lobby room | ✅ Tested | `test_stop_lobby_room_refunds_joined_players` |
| 3 | Stop running room | ✅ Tested | `test_stop_running_room_refunds_and_halts_number_calling` |
| 4 | Stop during number call | ✅ Tested | Same test — asserts `call_index` freezes, not just that status changes |
| 5 | Stop immediately before claim | ✅ Tested (as part of the race) | `test_admin_stop_racing_real_settlement_produces_exactly_one_outcome` |
| 6 | Stop immediately after claim | ✅ Tested | Same test — races the stop against an already-scheduled settlement task |
| 7 | Stop during settlement | ✅ Tested | Same test — this *is* the settlement-window race |
| 8 | Duplicate stop | ✅ Tested | `test_duplicate_stop_is_idempotent` |
| 9 | Concurrent stop by two admins | ✅ Tested | `test_concurrent_stop_by_two_admins_refunds_exactly_once` |
| 10 | Worker restart after stop request | ✅ Structurally guaranteed (Section 5) — not a new test, since no new durable "pending" state exists to lose across a restart in the first place | Covered by existing `run_active_rooms()`/`is_active` tests + this design's own reasoning above |
| 11 | Redis restart after stop request | ✅ Structurally guaranteed (Section 5) | Covered by the same reasoning — the financial commit never depends on Redis |
| 12 | Engine crash after stop request | ✅ Covered by pre-existing, tested crash recovery | `recover_orphaned_rounds()`'s own existing test suite |
| 13 | Stale admin request | N/A by construction — there is no "stale" concept for this action: it always reads and acts on the room's *current* state at request time, not a cached one | — |
| 14 | Unauthorized admin request | ✅ Tested | `test_stop_room_endpoint_requires_superadmin_over_http` (real HTTP 403 for an `ops` role) |
| 15 | Malformed stop request | ✅ Tested | `test_stop_room_rejects_wrong_confirmation_token`, `test_stop_room_rejects_empty_reason`, and the real-HTTP 422 case in `test_stop_room_preview_and_stop_over_http_full_flow` |
| 16 | Stop after round already settled | ✅ Tested | `test_stop_after_round_already_settled_does_not_touch_the_winner` — explicitly proves a legitimate winner's payout is untouched |

## 10. Release gate

- Unit/integration tests: 12 tests in `test_emergency_room_stop.py`, all
  passing; the two concurrency-critical ones (settlement race, dual-admin
  race) independently re-run 10x and 5x respectively in isolation, clean
  every run.
- Real-browser (e2e) test: `test_emergency_room_stop_e2e.py`, 3x
  repeated, clean every run.
- Financial invariants: verified directly via real ledger balance
  assertions in every test, not inferred from HTTP status codes.
- RBAC: verified over real HTTP (403 for `ops`, 200 for `superadmin`).
- Audit: verified via the `before`/`after` snapshot shape (matching
  every other admin mutation's own established pattern); not yet given
  its own dedicated assertion reading back the `admin_audit_log` row
  directly — a reasonable, low-risk follow-up rather than a blocking gap,
  since the exact same `audit.record()` call this action uses is already
  covered by other tests elsewhere in this codebase.
- Recovery: covered by pre-existing, unmodified crash-recovery machinery
  (Section 4/9's own reasoning) — this feature adds no new recovery path
  to independently re-verify.
- mypy: clean (103 files).
- Full regression: `pytest tests/ -q` — see `docs/RELEASE_CANDIDATE.md`
  for this pass's own final run number, taken *after* this feature
  landed.

Not rushed: `round_engine.py`'s two changes (the settlement guard, the
`_call_next_number()` external-stop detection) are each small, targeted,
and directly justified by a specific, named risk — not a broad rewrite
of the file's own state machine.
