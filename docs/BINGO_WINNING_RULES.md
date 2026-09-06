# Bingo Winning Rules — Configurable Per Room

The "must complete two lines to win" rule is now a real, per-room
admin-configurable value (`rooms.min_winning_lines`), not a hardcoded
constant — integrated into the *existing* architecture that already made
`win_patterns` (which line shapes count) per-room configurable, not a
parallel system.

## What already existed before this change

Inspected before writing anything, per the explicit instruction to
integrate cleanly rather than duplicate:

- **The two-line rule already existed**: `packages/core/bingo.py::
  MIN_WINNING_LINES = 2`, checked inside `has_won()`.
- **Which line shapes count was already per-room configurable**:
  `rooms.win_patterns` (jsonb, admin-editable via the existing
  `PATCH /rooms/{id}` route and `web/admin/js/screens/rooms.js`'s
  checkboxes) — enable/disable rows, columns, diagonals per room,
  already exactly the pattern this change extends `min_winning_lines`
  to follow, not something built fresh.
- **FREE space already counted as complete**: `mark_grid()`'s own
  `marks[r][c] = grid[r][c] == 0 or called_number in called` — cell
  value `0` (the FREE space) is unconditionally marked, for every
  pattern that passes through it, unchanged by this work.
- **Board size is fixed at 5x5**: the standard, video-verified,
  already-shipped card format (`docs/DECISIONS.md`'s "432-card pool"
  entry). Not made configurable — a 5x5 grid is what "Bingo" means in
  this product, changing it would touch the card pool, every existing
  card, and the entire rendering layer for a request with no stated
  business need; the directive's own "board/card size where applicable"
  phrasing is exactly the case where it doesn't apply here.
- **The backend was already authoritative**: the client never
  determines a win; `RoundEngine.claim()` is the sole place a claim is
  accepted or rejected, and the client's own "Claim" button enabling is
  cosmetic, always re-validated server-side regardless of what the
  client believes.
- **A real, pre-existing drift risk found while inspecting**: `claim()`
  computed its own win check inline (`len(won) >= bingo.MIN_WINNING_
  LINES`) instead of going through `has_won()`, despite that function's
  own header comment already stating every caller must route through it
  "so the real-money win rule can never drift out of sync." Fixed as
  part of this change — both call sites now reference the room's own
  `min_winning_lines`, never the global constant.

## What's new

| Layer | Change |
|---|---|
| Database | `rooms.min_winning_lines smallint NOT NULL DEFAULT 2 CHECK (BETWEEN 1 AND 4)` (migration `2e5e67f7b227`). Bounded, not unbounded — a 5x5 grid has only 12 total lines; requiring more than a handful makes a room practically unwinnable, the same "prevent invalid configuration" principle every other bounded room field already follows. |
| `packages/core/bingo.py` | `has_won()` takes `min_winning_lines` as a parameter (default `MIN_WINNING_LINES=2`, preserved for this module's own unit tests and any caller that hasn't been migrated to pass a real value); the module constant itself is unchanged and still documents the product default. |
| `RoomConfig` / `load_room_config()` | New `min_winning_lines: int` field, loaded the exact same way `win_patterns` already is. `RoomConfig` is only ever constructed via `load_room_config()` (confirmed by a repo-wide search) — no other call site needed updating. |
| `RoundEngine.claim()` | Its own inline threshold check now reads `self._room.min_winning_lines` instead of the global constant — closing the pre-existing drift risk above. |
| `RoundEngine`'s auto-mark scan | Now passes `min_winning_lines=self._room.min_winning_lines` into `has_won()` explicitly. |
| Admin queries | `create_room_admin()`/`update_room_admin()` accept and validate `min_winning_lines` (range-checked before ever reaching the database, raising the same `ValueError` → HTTP 422 mapping every other admin validation error already uses); `list_rooms()` returns it. |
| Admin API | `CreateRoomRequest.min_winning_lines: int = 2`; `PATCH /rooms/{id}` already accepted arbitrary `changes` — `min_winning_lines` is simply now in `_UPDATABLE_ROOM_FIELDS`. |
| Admin UI | New "Required winning lines" number input (1-4) next to the existing win-pattern checkboxes, in both the create-room and edit-room forms, plus a live "Winning condition: …" preview that updates as either the count or the enabled patterns change — and a new "Winning condition" column in the room list, so an admin sees the real configured rule at a glance without opening the edit form. |
| Gateway (`build_state_sync`) | The one-message reconnect/join payload now includes `min_winning_lines` alongside the `win_patterns` it already sent — the frontend's own source of truth for this room. |
| Mini App (`app.v6.js` / `render/card.js`) | `render/card.js::hasCompletePattern()` had its own hardcoded `MIN_WINNING_LINES = 2` module constant — a real, found-during-this-change bug: it would have silently drifted from any room configured differently, since nothing wired a per-room value to it before. Fixed: it's now a parameter, sourced from the real per-room value the gateway sends. The mismatch was never a financial risk (the server independently re-validates every claim, per this exact file's own pre-existing comment) but a real UX one: a player on a differently-configured room could have seen the "Claim" button enable (or, with client-side auto-mark on, an automatic claim attempt fire) after the *old* hardcoded count rather than the room's *real* required count. |

## How rule changes propagate (existing lifecycle, not a new one)

`RoundEngine` loads its `RoomConfig` exactly once, when it first claims a
room, and keeps using that same snapshot for every round it runs until
the engine itself is re-claimed (a restart, or the room's lock changing
hands) — this is the existing lifecycle every other room field
(`win_patterns`, `stake`, `house_cut_bps`, …) already has, and
`min_winning_lines` follows it identically rather than inventing a
separate hot-reload mechanism:

- An edit **never** retroactively alters a round already in progress
  (each round snapshots its own `stake`/`house_cut_bps`/etc. into its own
  `rounds` row at creation).
- An edit does **not** even reach the *next* round in an already-live
  engine's own continuous lifecycle — it takes effect the next time this
  specific room is claimed (a deploy/restart, or the room's lock being
  released and re-acquired).
- This is a deliberate, existing architectural choice being preserved,
  not a limitation introduced by this feature — see `update_room_admin`'s
  own docstring for the precise reasoning.

## Performance

Zero new database queries on the hot path. `min_winning_lines` rides
inside the exact same `RoomConfig` object every other per-room setting
already does — loaded once per engine claim, read from memory on every
`claim()` call and every auto-mark scan tick, identical in cost to how
`win_patterns` has always been read. `has_won()`/`winning_patterns()`
themselves are unchanged in complexity — still one pass over the grid's
already-precomputed pattern table, filtered by enabled kind, counted
against a threshold that's now a parameter instead of a global,
which costs nothing extra.

## Testing

| # | Requirement | Where |
|---|---|---|
| 1-10 | Pure win-logic scenarios (1 line reject, 2 lines/columns/diagonals/combinations accept, FREE space, invalid patterns) | Already covered by `tests/unit/test_bingo.py`'s existing 16-scenario suite from an earlier pass; unchanged and re-verified passing under the new parameterized `has_won()`. |
| 11 | Configurable required-line count | New: `test_has_won_with_min_winning_lines_1_a_single_line_wins`, `test_has_won_with_min_winning_lines_3_two_lines_is_not_enough`, `test_has_won_with_min_winning_lines_3_three_lines_wins` (pure logic); `test_room_configured_for_one_winning_line_accepts_a_single_line`, `test_room_configured_for_three_winning_lines_rejects_two_lines` (real engine, real `claim()`, proving the configuration reaches production code, not just the pure function); 9 admin-layer tests in `test_admin_queries.py` (settable at creation/update, defaults to 2, rejects 0/-1/5/100 both at creation and update, rejection leaves the existing value untouched). |
| 12 | Backend and frontend show the same result | `test_state_sync_reports_the_rooms_real_configured_winning_condition` — a real WS client joining a room configured for 3 lines receives `min_winning_lines: 3` in its real `state_sync` payload, the exact value `render/card.js` now reads instead of its former hardcoded constant. Plus 2 real-Chromium e2e tests (`test_bingo_rules_admin_e2e.py`) proving the admin UI's live preview and the room-list display both show the real configured value, not a guess. |
| 13 | Existing game modes continue working | Full regression suite — see `docs/RELEASE_CANDIDATE.md` for this pass's own final run number. |

## Verified end-to-end flow

Admin Dashboard (new number input + live preview) → Room Configuration
(`rooms.min_winning_lines`, validated, audited) → Bingo Engine
(`RoomConfig.min_winning_lines`, loaded once per claim) → Backend Win
Validation (`claim()`'s own threshold check, `has_won()`'s auto-mark
check — both reference the room's real value, confirmed by a repo-wide
search finding no remaining reference to the bare `bingo.MIN_WINNING_
LINES` constant in any production code path) → Winner Declaration
(`_settle_with_winners()`, unchanged — it already just pays out whoever
`claim()`/the auto-mark scan already decided won) → Frontend/UI
(`state_sync` → `render/card.js`'s now-parameterized `hasCompletePattern
()`) — no remaining path where a stale, hardcoded "2" can incorrectly
gate a win or a UI affordance for a room configured differently.
