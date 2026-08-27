# Decisions

Where an implementer (human or AI) deviates from `idea.md`, or makes a call
`idea.md` leaves open, it goes here with the reasoning. Newest first.

---

## 2026-08-27 — `/docs`, `/redoc`, `/openapi.json` were bypassing the admin IP allowlist too

Reconsidering the previous entry's deferred "two independent IP
-allowlist enforcement mechanisms" finding rather than just leaving it
noted: refactoring the mechanism itself stayed correctly off-limits (no
demonstrated current bug, touches working security-critical code), but
the underlying *concern* -- "a route can bypass the allowlist and
nobody notices" -- was checkable directly, by actually enumerating
`app.routes` instead of grepping for hand-written routes the way the
`/metrics` and `/auth/login` fixes were each found before.

That enumeration found three: FastAPI's own auto-added `/docs`
(Swagger UI), `/redoc`, and `/openapi.json`. None of them show up in a
search for routes anyone wrote by hand -- they don't exist as decorated
functions in `services/admin/app.py` at all, `FastAPI()` adds them
implicitly -- so the exact search pattern that caught the two earlier
gaps would never have found these. Confirmed live, not assumed: with a
real IP allowlist configured excluding the caller, `/dashboard` and
`/metrics` correctly return 403 while `/docs`, `/openapi.json`, and
`/redoc` all still returned 200 -- a real-money admin panel's entire API
surface (every route, every request/response field) reachable by anyone
on the network regardless of the allowlist spec 9.2 asks the whole
panel to have.

**Fixed**: extended the existing `_console_frontend_ip_allowlist`
middleware (renamed `_unauthenticated_route_ip_allowlist`, since its
scope is no longer just `/console`) to also cover these three paths
(plus `/docs/oauth2-redirect`, the OAuth2 redirect helper FastAPI's docs
UI itself can add). Same fix shape as the two earlier gaps -- extend the
existing check to a path it didn't cover -- not a new mechanism.

**Verification**: `test_fastapis_own_docs_routes_are_reachable_with_no_
allowlist` and `test_fastapis_own_docs_routes_are_blocked_by_the_ip_
allowlist` (the latter confirmed to genuinely fail -- 200, not 403 --
against the pre-fix file via the usual stash-revert step). Full
clean-slate rebuild: `docker compose down -v` -> `up -d` -> `alembic
upgrade head` -> `mypy` clean (65 source files) -> full default suite
753 passed (up from 751), 18 deselected -> `-m load` 1 failure
(`test_gateway_fanout.py`'s stalled-reader test this time, the same
well-documented host-contention pattern, confirmed via `uptime`/`docker
ps`, unrelated to this turn) -> `-m chaos_infra` 2 passed -> `-m e2e` 11
passed.

---

## 2026-08-27 — A `/code-review high` pass over today's own work: three real findings fixed, three deliberately deferred

With the explicit task list and every follow-on gap this session found
on its own now closed, the next safe task was a fresh `/code-review`
pass -- first scoped to just the latest commit (found nothing but a
cosmetic unused-variable nit matching this codebase's own established
`_, username, password, totp_secret = ...`-style convention, not worth
touching), then rerun scoped to the whole day's work (`HEAD~7..HEAD`:
the KYC writer, risk-screen backend, admin frontend, age gate, gateway
chaos test, and CD fix). Three independent bug-hunting angles came back
with zero correctness findings -- everything already built today
checked out as correctly implemented and correctly wired. The eight
findings that did surface were efficiency, reuse, and architecture
observations. Three were real and cheap enough to fix now; three were
real but deliberately left alone, for reasons worth recording rather
than silently skipping.

**Fixed:**
- **`repeat_room_pairings()`'s missing index** (migration
  `d4dfad3a4fb2`). The function's own docstring claims its `since_days`
  window keeps the query bounded as `round_entries` grows -- but
  `round_entries` had no index on `joined_at` at all, only `PRIMARY KEY
  (round_id, card_no)` and `UNIQUE (round_id, user_id)`, so every call
  still forced a full sequential scan of a table that grows with every
  stake ever made platform-wide. The claim in the docstring wasn't
  actually true. Added `ix_round_entries_joined_at`.
- **`request_withdrawal()`'s four sequential queries under a row lock**
  consolidated to two. `recent_deposit` stays separate and early
  (it can reject the whole request *before* `ledger.post()` ever
  touches money -- deliberately not merged with the others, which only
  ever affect auto-approve-vs-review routing after funds are already
  locked either way). `lifetime_in`, `lifetime_out`, and the new
  `recent_withdrawal_count` merged into one `FILTER`-based query, the
  same pattern `services/admin/queries.py`'s own `dashboard_summary()`
  already established for exactly this shape of "three near-identical
  scans over the same rows." A pure, behavior-preserving refactor --
  verified by the existing test suite passing identically before and
  after, the same standard this session applies to every non-bug-fix
  optimization, not the stash-revert-confirm-fail cycle a real fix gets.
- **Three inconsistent copies of the same jsonb-as-string workaround**
  (`list_rooms()`, `_room_audit_value()`, and `shared_payout_account_
  clusters()` -- the last one missing the `isinstance` guard entirely,
  a real latent bug waiting for the day a jsonb codec ever gets
  registered on this pool) consolidated into one `_parse_jsonb()`
  helper, safe to call unconditionally on an already-parsed value.

**Deliberately deferred, not silently skipped:**
- **Two independent IP-allowlist enforcement mechanisms** (the
  `_console_frontend_ip_allowlist` middleware added for `/console`, and
  the pre-existing per-route `_check_ip_allowlist()` calls) instead of
  one unified check. Real architectural debt -- this exact class of bug
  (a new unauthenticated route forgetting the check) has already
  happened twice per this file's own comments -- but unifying it means
  touching working, security-critical, already-well-tested code with no
  demonstrated current bug, which this session's own standing
  instruction is explicit about not doing without being asked. Left as
  documented debt, not a stealth rewrite.
- **No stored reason for why an auto-review outcome happened.** An
  admin looking at the review queue can't tell whether the velocity
  gate, the KYC threshold, or the lifetime-balance check was what
  triggered it without manually re-deriving it. Real, but shaped like a
  small feature addition (a new column, new write path) rather than a
  bug fix -- left open rather than expanded into unrequested feature
  work.
- **The admin frontend's repeated error-banner/action-handler
  boilerplate** (the same `try { ... } catch (err) { container.innerHTML
  = ...err.detail...}` shape copy-pasted 8-13 times across `web/admin/
  js/screens/*.js`, and three near-identical action handlers in
  `users.js`). Real duplication, but cosmetic/maintainability, not a
  correctness issue, and refactoring seven JS files touching every
  admin screen would need the same real-browser re-verification pass
  each screen already got once -- lower priority than the three fixes
  above, left for a dedicated pass rather than done partially here.

**Verification**: full clean-slate rebuild -- `docker compose down -v`
-> `up -d` -> `alembic upgrade head` (new index migration applies
cleanly) -> `mypy` clean (65 source files) -> the three affected test
files run directly first (`test_payments_withdrawals.py`, `test_admin_
queries.py`, `test_admin_app.py`, 63 passed, confirming the two
behavior-preserving refactors produce identical results) -> full
default suite 751 passed, 18 deselected (unchanged, as expected for
behavior-preserving changes) -> `-m load` 1 failure
(`test_gateway_fanout.py`, the same well-documented host-contention
pattern, confirmed via `uptime`/`docker ps`, unrelated to this turn) ->
`-m chaos_infra` 2 passed -> `-m e2e` 11 passed.

---

## 2026-08-27 — Permanent real-browser coverage for the admin console frontend

With every explicit boundary item from the status audit now genuinely
blocked on the user (a decision, GitHub access, external connectivity,
or a lawyer), the next safe task came from applying this session's own
just-proven lesson -- verify a status claim against reality instead of
trusting it -- one more time, to a different area: `web/admin/` (the
admin console frontend, shipped two commits ago) had real, thorough
manual verification at the time (a Playwright walkthrough, screenshots,
a genuine CSS bug caught and fixed), but that verification script was
never committed. `tests/integration/test_admin_app.py`/`test_admin_
queries.py` only ever exercised the API layer. The whole frontend has
had zero permanent regression coverage since the moment it shipped --
the same gap `test_miniapp_e2e.py`/`test_miniapp_wallet_e2e.py` already
closed for the player-facing frontend, just never closed here.

**What was built**: `tests/integration/test_admin_console_e2e.py`,
matching `test_miniapp_e2e.py`'s own established pattern exactly (real
Chromium via the shared `browser` fixture, the real `admin_server`
in-process app, no mocked DOM) -- four tests: login through the actual
form lands on a genuinely-hidden login screen and a real dashboard with
zero JS errors; a full users-search-to-KYC-action round trip through
the UI that confirms the database row actually changed, not just that a
toast appeared; an RBAC-denied screen showing a real, specific error
(not a blank page); and logout returning to the login screen.

**A real bug in the test itself, caught by running it repeatedly, not
once**: the KYC-action test's `wait_for_function` polled `document.
getElementById('kyc-select').value === '2'` -- `users.js`'s own
`loadDetail()` briefly clears and rebuilds the whole detail panel after
a successful action, so the element is genuinely `null` for a moment,
and Playwright surfaces that `TypeError` as a real test error rather
than treating it as "still false, keep polling." Fixed with optional
chaining (`?.value`), the actual fix for the actual race, not a longer
timeout papering over it. Confirmed by running the full file 5
consecutive times after the fix with zero failures (it had failed
roughly one run in three before).

**A second thing this caught, in the RBAC test**: the first draft
asserted the error banner contained the word "access," assuming `js/
app.js`'s generic 403 fallback message would show. It doesn't --
`js/screens/risk.js` catches its own fetch error and renders the real
backend detail directly (`"role 'support' lacks 'risk:view'"`), which
is more useful to an admin than a generic message would be. Not a bug;
the test's assumption was wrong, not the app -- fixed the assertion to
match the real, correct, already-documented-in-code behavior.

**Verification**: full clean-slate rebuild -- `docker compose down -v`
-> `up -d` -> `alembic upgrade head` -> `mypy` clean (64 source files)
-> full default suite 751 passed, 18 deselected (up 4 for the new
`e2e`-marked tests) -> `-m load` 1 failure (`test_load_multiroom.py`,
the same well-documented host-contention pattern, confirmed via
`uptime`/`docker ps`, unrelated to this turn) -> `-m chaos_infra` 2
passed -> `-m e2e` 11 passed on the clean rerun, after one unrelated
transient flake in `test_miniapp_full_gameplay_flow` (a pre-existing
test this turn never touched) reproduced as passing cleanly both alone
and in a full clean rerun -- the same shared-host-contention pattern
extended to a gameplay-timing assertion instead of a raw latency budget,
not a regression.

---

## 2026-08-26 — CD was actually broken by a real bug, not just waiting on the runner; fixed

Moving to the infrastructure item from the deep-read audit ("CD needs
the self-hosted runner registered"), the instruction was explicit: don't
modify production infrastructure blindly, inspect first. Inspecting
meant actually checking GitHub's own record of what happened, not
re-reading the workflow file and assuming it was correct because it
looked reasonable -- `gh run list` and `gh run view --log` against this
repo's real Actions history, not a guess.

That inspection contradicted the earlier status report. Every CD run
since the pipeline was added (4 for 4) had failed -- but not in the
`deploy` job the "just needs a runner" framing implied. `deploy` had
never even run; it showed `skipped` every time, because `build-and-push`
-- a plain GitHub-hosted job, no runner involved at all -- was failing
first, on every single run, with:
```
ERROR: failed to build: invalid tag "ghcr.io/Nebyudejenie/game:<sha>":
repository name must be lowercase
```
GHCR (like every OCI registry) requires an all-lowercase repository
name; `${{ github.repository }}` preserves this repo's real casing
(`Nebyudejenie/game`), and nothing lowercased it before it became half
of a Docker tag. A real, live bug this session's own earlier status
report missed -- the CD workflow was checked for existing (it does) and
its setup steps documented (they were, accurately), but never checked
against its own actual run history.

**Fixed**: `.github/workflows/cd.yml`'s `build-and-push` step now
lowercases `github.repository` (`tr '[:upper:]' '[:lower:]'`) into a
local `REPO` variable before building either tag. `docker-compose.prod.
yml` only ever consumes the already-built `JOBINGO_IMAGE` string as an
opaque value (confirmed by reading it), so this one fix is the complete
fix, not a partial one needing a second change elsewhere. Verified with
`actionlint` (downloaded fresh, the same real static analyzer this
session's CI/CD work used originally) against both workflow files --
clean -- and the lowercase transformation itself run directly in a
shell to confirm it produces the exact tag GHCR requires
(`ghcr.io/nebyudejenie/game:<sha>`). Not pushed to `main` and not
run against real GitHub Actions -- per this session's standing
discipline, commits stay local; the user pushes independently, and the
next real push will be the actual end-to-end proof this fix works.

**Two smaller doc bugs caught in the same pass, fixed alongside**:
`docker-compose.prod.yml`'s own header comment pointed at a
`.env.example` file that doesn't exist (the real one is
`.env.prod.example`) and a README "Deploying" section that doesn't
exist either (the real heading is "CI/CD") -- both corrected. Also
added the one env var `.env.prod.example` was missing relative to what
`packages/core/config.py` actually reads: `MAX_WITHDRAWALS_PER_DAY`
(this session's own withdrawal-velocity gate), documented the same way
every other has-a-default-but-worth-showing value already is in that
file.

**What's actually still open, confirmed via the GitHub API directly**
(`gh api repos/.../actions/runners`, `.../environments`), not assumed:
zero self-hosted runners registered, zero environments configured. Both
require the user's own GitHub account access (Settings -> Actions ->
Runners; Settings -> Environments) -- genuinely outside what this
session can do, not deferred out of caution. Whether `deploy` itself
has any *further* problems beyond the runner is honestly unknown: it
has never once run far enough to find out, blocked first by the bug
above. The runner is the next real blocker as far as static inspection
can tell, not a guarantee everything past it is already proven correct.

---

## 2026-08-26 — Gateway-kill reconnect chaos test (spec 10.3), and how its socket count was actually chosen

The second buildable-now item from the deep-read status audit: spec
10.3's load/chaos target table has six scenarios; five already had a
real test (engine crash, Redis restart, duplicate webhook × 100, 1,000
-player rush, multi-room fan-out). "Kill a Gateway pod with 8,000
sockets -> clients reconnect within 5s with correct state" had none --
the client-side reconnect logic (`web/miniapp/js/ws.js`'s backoff+jitter)
was built and unit-tested in isolation, but nothing proved the actual
end-to-end mechanism against a real killed process.

**What was built**: `tests/integration/test_chaos_gateway_kill.py`.
Two genuinely separate `services/gateway/app.py` OS processes (`asyncio.
create_subprocess_exec("uvicorn", ...)`, not the in-process `uvicorn.
Server` `test_gateway_fanout.py`'s `gateway_server` fixture shares the
test's own event loop with -- that one can't be sent a real kill signal
while the test itself keeps running). Both start healthy before any
client connects, matching spec 10.2's own scaling assumption ("Add
Gateway replicas. Stateless, linear.") -- a real fleet already has more
than one replica running; a dead one doesn't need a cold boot to
recover from, a load balancer just routes to whichever is already up.
A batch of real, authenticated WebSocket connections lands on process
A, gets real state via `build_state_sync()`, then A is `SIGKILL`'d --
no graceful shutdown, no chance for `close_for_shutdown()` to run, the
same unclean-death semantics a pod eviction has. Every client then
reconnects to process B, a process with zero shared memory of process A
or any of these connections, and must get the *correct* state (room_id,
stake) straight from Postgres -- the actual thing `services/gateway/
app.py`'s own docstring already claims ("any replica can serve any
player") and the actual thing this test proves for real instead of by
assertion.

**Honesty about scale -- the actual reasoning, not just the number**:
8,000 real concurrent sockets is not achievable in this sandbox (a
shared 4-core host that already shows confirmed contention-driven
latency issues at 1,000 sockets in the existing load tests). The
question was what scale this environment could prove *reliably*, not
just once. Measurement, not guesswork, answered it: 300 sockets measured
3.6-4.8s against the 5s budget across repeated runs -- technically
passing, but with under 10% margin, exactly the shape of test that flakes
the first time this shared host has a bad five minutes (which, per this
session's own repeated `uptime`/`docker ps` checks, happens often here).
Profiling at 50 and 100 sockets showed the cost is dominated by fixed
per-run overhead (subprocess and connection setup under real host
contention), not socket count -- 50 sockets measured about the same
~2.5-3.8s as 100 did. So 300 sockets bought no extra confidence, only
less margin. Settled on 50: a real, meaningfully large, genuinely
concurrent scale, with consistent ~35-50% margin under budget even
under this session's own observed elevated load (`uptime` load average
above 3.5 during profiling). Not the spec's 8,000; said so in the test's
own module docstring, not just here.

**A real timing-methodology fix caught along the way**: the first draft
gated the reconnect-timer's start on first confirming every one of
process A's sockets had noticed the closure (`ConnectionClosed` on
`recv()`). That's not what a real client does -- a real socket's
`onclose` fires independently and starts *that* client's own reconnect
immediately, it never waits for every other socket to also confirm
closure first. Restructured so the "prove the kill was real" check and
the actual reconnect race run concurrently, only the reconnect side
gates the measured number -- a more accurate number, and incidentally
not a smaller one (the confirmation step turned out not to be the
dominant cost), but the right thing to measure regardless of which way
it moved the result.

**Verification**: the test's own assertions are the proof (a real
`SIGKILL`, a real second process, real Postgres-sourced state
comparison) -- there's no prior "unfixed" version of this scenario to
revert to and confirm fails, the same new-feature reasoning applied to
the KYC and Risk-screen entries below. Run in isolation 7+ times during
development (30/50/100/300-socket variants) and 3 more times at the
final SOCKET_COUNT=50 after settling on it, consistently 2.2-3.8s
against the 5s budget. Also run together with `test_chaos_redis_restart.
py` under the shared `chaos_infra` marker (both genuinely independent --
this test doesn't touch the shared `redis`/`pool` fixtures at all, only
`conn` plus its own dedicated subprocesses -- confirmed safe regardless
of run order relative to the Redis-restart test's own fixture-breaking
side effect). Full clean-slate rebuild: `docker compose down -v` ->
`up -d` -> `alembic upgrade head` -> `mypy` clean (64 source files) ->
full default suite 751 passed, 14 deselected (up one from the new
`chaos_infra`-marked test) -> `-m load` 2 failures, the same
well-documented `test_gateway_fanout.py`/`test_load_multiroom.py`
host-contention pattern, confirmed via `uptime`/`docker ps` at the time
and unrelated to this turn's changes -> `-m chaos_infra` 2 passed (this
new test plus the existing Redis-restart test, run together) -> `-m e2e`
7 passed.

---

## 2026-08-26 — 18+ age-gate self-declaration at registration (spec section 12)

A deep-read status audit against every section of the spec (not just this
session's own summarized memory of prior work) surfaced one real,
previously-undocumented gap: spec 12's age-gate bullet is actually two
separate controls -- "18+ declaration at registration, ID verification
at KYC level 2" -- and only the second half had ever been tracked
(`kyc_level`'s missing-writer gap, closed earlier this session, plus its
own still-open document-verification-method decision). The first half, a
plain self-declaration checkbox-equivalent at registration, simply never
existed: no column recorded it, no prompt text mentioned it.

**What was built**: the smallest correct integration point, not a new
mechanism. `services/bot/registration.py`'s `register_from_contact()` --
the one function that actually completes registration, on both of its
write paths (a brand-new `users` row, and attaching a phone to an
already-existing phoneless row left by the gateway's lazy
`get_or_create_user_by_telegram_id()`) -- now sets a new
`users.age_confirmed_at` timestamp (migration `d812e3d87349`) the moment
registration first completes, `COALESCE`d against the existing value on
both paths so a later idempotent re-registration can never overwrite the
original declaration. The `register.prompt` i18n string (both `am.json`
and `en.json`) shown alongside the existing share-contact button now
states the 18+ requirement explicitly and frames sharing the contact as
the confirmation -- the same "by continuing you confirm..." pattern most
consent flows use, not a separate button, a new Redis pending-state
module, or aiogram's FSM (none of which this bot uses anywhere else, and
introducing one for a single declaration step would be a new mechanism
where the existing single-message-plus-existing-button flow already
says what's needed).

**Deliberately not built**: an explicit "I am under 18" rejection path.
Spec 12 asks for a declaration, not a hard input gate -- someone who
isn't 18+ simply doesn't proceed, the standard shape for this kind of
consent. Nothing here touches KYC-level identity verification, which
stays exactly as open as the entry below already documents.

**A real, honest translation caveat**: the Amharic declaration text was
written carefully but has not been reviewed by a native Amharic speaker
or legal/compliance counsel -- for a string with actual regulatory
weight, that review should happen before this reaches real users, the
same way the whole platform's NLA licensing question stays explicitly
unclaimed as "done" anywhere in this repo.

**Verification**: `test_new_registration_records_an_age_confirmation_
timestamp`, `test_completing_a_phoneless_row_also_records_age_
confirmation`, and `test_re_registering_does_not_reset_the_original_age_
confirmation_timestamp` in `tests/integration/test_registration.py`;
`test_start_registration_prompt_includes_the_18_plus_declaration` in
`tests/integration/test_bot_handlers.py` (a real end-to-end run through
aiogram's Dispatcher against a fake Telegram session). All four confirmed
to genuinely fail against the pre-change files via the usual stash
-revert step. The handler-level test also caught a real, pre-existing
test-infrastructure fragility along the way: `_settle()`'s fixed 50ms
sleep is already marginal for a two-message flow given `Notifier`'s own
enforced ~40ms inter-message pacing gap, and flaked under this sandbox's
documented host contention when this test ran as part of the full file
rather than alone. Fixed locally in the one test that actually needs
both messages (a real deadline poll instead of a fixed sleep), not by
touching the shared `_settle()` helper 20+ other, single-message-only
tests already rely on. Also caught: `tests/unit/test_i18n.py::test_
lookup_in_amharic` hardcoded the old exact `register.prompt` string as
its lookup-mechanism example; updated to the new real value, since that
test was never asserting anything about the *content*, only that
`i18n.t()` retrieves it correctly.

Full clean-slate rebuild: `docker compose down -v` -> `up -d` ->
`alembic upgrade head` (new migration applies cleanly from a fresh
database) -> `mypy` clean (64 source files) -> full default suite 751
passed (up from 747), 13 deselected -> `-m load` 2 failures, both the
same well-documented `test_gateway_fanout.py`/`test_load_multiroom.py`
host-contention pattern (confirmed via `uptime`/`docker ps` at the time,
load average ~2.2, unrelated `santim-commerce-*`/`spos-*` containers
active; this turn touched no gateway/fanout code) -> `-m chaos_infra` 1
passed -> `-m e2e` 7 passed.

---

## 2026-08-26 — Admin console web frontend (`web/admin/`), mounted at `/console`

The last major gap from the project-completion audit that also produced
the KYC and Risk-screen entries below: `services/admin/app.py` has been a
JSON API only since Phase 7, with every screen idea.md's own admin-panel
table (§6231: dashboard, users, rounds, rooms, payments, reports, risk,
audit log) describes never actually reachable except via curl or a test
client. Built the real frontend for it.

**Approach**: `web/admin/` -- plain HTML/CSS/vanilla JS, ES modules, no
framework and no build step, the exact same approach `web/miniapp/`
already established for the player-facing Mini App rather than
introducing a second, inconsistent frontend stack for one more surface.
One shell page (`index.html`) with a login screen and an app screen;
`js/app.js` toggles between screen modules (`js/screens/*.js`, one per
nav item) that each own their own render/fetch/error-handling, calling
straight back into the same-origin admin API (`js/api.js`'s thin `fetch`
wrapper, bearer token in `localStorage`). Covers all eight nav items:
dashboard, users (search, detail, adjust balance, set status, set KYC
level, ledger history), payments (withdrawal review queue, approve
/reject), rounds (list, detail, fairness verification, void), rooms
(list, create, activate/deactivate), reports (GGR, LTV, retention),
risk (both screens from the entry below), and the audit log.

**Mounted at `/console`, not `/`**: `services/admin/app.py` already
serves a JSON API from `/`, so the frontend needed its own path (unlike
`services/gateway/app.py`, which has nothing else living at `/` for the
Mini App's own static mount to collide with). Protected by a new
`_console_frontend_ip_allowlist` middleware, not by `Depends()` the way
every API route is -- a plain `StaticFiles` mount has no dependency
-injection point of its own to run the allowlist check through, which
is exactly the same gap `/metrics` and `/auth/login` were each already
caught with in earlier passes (see their own code comments). Spec
section 9.2 asks the *whole* admin panel to sit behind an IP allowlist,
not just its API half.

**A real bug this caught**: the first real-browser pass (not just curl)
found that the login screen never actually disappeared after a
successful login -- `#login-screen` and `#app-shell` toggle via the
`hidden` attribute in `app.js`, but `admin.css` gave each an
unconditional `display: flex`/`display: grid` rule keyed off a bare ID
selector, which outranks the browser's own `[hidden] { display: none }`
(an ID selector beats an attribute selector on specificity) -- so
setting `.hidden = true` silently did nothing and the login card stayed
stacked on top of the dashboard underneath it. Fixed by scoping both
rules to `:not([hidden])` instead of fighting the attribute's own
specificity. Exactly the kind of bug curl or an API-level test would
never catch, and the reason this session's own discipline requires a
real browser pass for UI work, not just a green test suite.

**Verification**: a real Playwright walkthrough (Chromium, the same
browser this repo's own e2e tests already use) against the live dev
database: create a real admin user, log in through the actual form
(username + password + real TOTP code), visit all eight screens, run a
real search that returns real users, open a user's detail panel, submit
the KYC-level action through the UI and confirm both the toast and the
reloaded panel reflect the change, log out and confirm the login screen
reappears. Zero console/page errors other than the browser's own
automatic (and harmless) `/favicon.ico` request. Two new automated
regression tests mirror the existing `/metrics`/`/auth/login` allowlist
tests: `test_console_frontend_is_reachable_with_no_allowlist`,
`test_console_frontend_is_blocked_by_the_ip_allowlist` -- the latter
confirmed to genuinely fail (404, no mount at all) against the
pre-change file. Full clean-slate rebuild: `docker compose down -v` ->
`up -d` -> `alembic upgrade head` -> `mypy` clean (63 source files) ->
full default suite 747 passed (up from 745), 13 deselected -> `-m load`
2 failures, both `test_gateway_fanout.py`/`test_load_multiroom.py`
latency-budget tests exceeding budget under confirmed real host
contention (`uptime` load average ~1.3, unrelated `santim-commerce-*`/
`spos-*` containers active) -- the same well-documented pattern from
throughout this session, and this turn touched no gateway/fanout code
at all -> `-m chaos_infra` 1 passed -> `-m e2e` 7 passed.

---

## 2026-08-26 — Risk screen backend (spec 8.4/6231) and the missing withdrawal-velocity gate

Continuing the same project-completion audit that found the KYC gap
below, spec 6231 lists a "Risk" admin nav item ("Collusion clusters,
multi-account links, flagged withdrawals") and spec 8.4 lists six
specific anti-fraud rules to encode -- against the actual codebase, zero
of that existed: no `risk_flags` writer, no clustering query, and one of
the six 8.4 rules ("Withdrawal velocity > 3/day -> Review") had no
corresponding check anywhere in `withdrawals.py`'s own auto-approve
logic, the same kind of real, live enforcement gap as the KYC finding
below rather than just an unbuilt screen.

**Withdrawal velocity gate (real bug, not a scope gap)**: added a
`recent_withdrawal_count < max_withdrawals_per_day` (default 3, new
`Settings.max_withdrawals_per_day`) condition to `request_withdrawal()`'s
`auto_ok` computation in `services/payments/withdrawals.py`, counting
every `direction = 'out'` payment row in the trailing 24 hours regardless
of its own outcome -- a burst of requests is itself the suspicious
signal, not just the ones that happened to succeed. Verified by the
stash-revert-confirm-fail step this codebase always applies to an actual
behavior fix: `test_withdrawal_velocity_over_the_daily_limit_goes_to_
review` genuinely fails (`'approved' == 'review'`) against the unfixed
file before the two-line `auto_ok` change, confirming the test is real
and not accidentally passing regardless.

**Risk-screen queries (new, read-only reports)**: two of 8.4's rules
were buildable right now from data this codebase already collects, so
they were, matching the exact "live SQL query, no materialized table, no
background job" pattern `top_players_by_ltv()`/`retention_cohorts()`/
`daily_ggr()` already established for every other admin report:
- `shared_payout_account_clusters()` -- "Same payout account across
  multiple accounts -> Link accounts, flag cluster." Groups
  `payment_methods` by `account_ref`.
- `repeat_room_pairings()` -- "Winner and loser in the same room
  repeatedly, same pairs -> Collusion investigation." For every pair of
  users who've shared a round at least `min_shared_rounds` times (default
  3) in a trailing window (default 30 days, to keep the pairwise self
  -join bounded -- a 100-player room already has ~4,950 pairs per round),
  reports how many of those shared rounds each side won. Deliberately a
  data screen, not an automatic verdict -- the spec's own word is
  "investigation," so which pairs actually look suspicious stays an
  admin's judgment call, the same deliberate stance already documented in
  README.md for the risk-score and holder-name-match gaps.
Both wired to `GET /risk/shared-payout-accounts` and `GET
/risk/repeat-pairings`, gated by a new `risk:view` permission scoped to
`{ops, finance, superadmin}` (the roles who'd actually act on what a risk
screen shows), not `reports:view`'s broader read-only set.

**Deliberately not built**: device-fingerprint clustering (8.4's other
account-linking rule) and a `risk_flags` storage table. Fingerprinting
has no writer anywhere in this codebase because nothing in the Mini App
collects a device fingerprint in the first place -- which library, and
what data it's allowed to touch under Ethiopian data-protection norms, is
a real, separate, not-yet-made product decision, not invented here, the
same category of deliberate deferral as the KYC document-collection
method below. A stored `risk_flags` table (spec 4.5 sketches one) was
skipped in favor of the live-query pattern above because nothing in this
codebase ever writes to one yet, and adding it now would mean also
building a background scan job and a flag-review workflow (mark
reviewed/dismissed) for uncertain benefit over just querying live --
revisit if/when an admin frontend actually exists to make a persistent,
stateful queue worth having.

**Verification**: two withdrawal tests (the velocity one above plus
`test_withdrawal_velocity_only_counts_the_trailing_24_hours`, confirming
a 2-day-old burst doesn't permanently wedge a normal user into review),
two query-level tests (`test_shared_payout_account_clusters_finds_users_
sharing_a_payout_destination`, `test_repeat_room_pairings_flags_a_
lopsided_recurring_pair` -- the latter also proves a single shared round
with a third user does *not* clear the threshold), and two RBAC tests
over real HTTP (`test_rbac_support_cannot_view_risk_screen_over_http`,
`test_rbac_ops_can_view_risk_screen_over_http`). Full clean-slate
rebuild: `docker compose down -v` -> `up -d` -> `alembic upgrade head` ->
`mypy` clean (63 source files) -> full default suite 745 passed (up from
739), 13 deselected -> `-m load` 5 passed, clean this time (no host
-contention flake) -> `-m chaos_infra` 1 passed -> `-m e2e` 7 passed.

---

## 2026-08-26 — Admin action to set `users.kyc_level`, closing the gate's missing writer

A project-completion audit against the original spec surfaced a real, live
gap rather than just an unbuilt feature: `users.kyc_level` (`smallint NOT
NULL DEFAULT 0 CHECK (kyc_level BETWEEN 0 AND 2)`) already had a real
consumer -- `services/payments/withdrawals.py`'s own `kyc_threshold` gate,
which blocks large withdrawals above a threshold unless the requesting
user's `kyc_level` clears it -- but no writer anywhere in the codebase.
Grepping the whole tree turned up nothing that ever set the column except
a raw `UPDATE users SET kyc_level = 2` living directly inside the one
existing test that needed a level-2 user
(`test_kyc_verified_user_can_withdraw_above_threshold`), standing in for
a real code path that didn't exist. Any actual user who needed KYC to
clear a large withdrawal had no path through the gate at all -- not even
a slow, manual one.

**What was built**: `services/admin/queries.py::set_kyc_level()`, matching
the exact audited-mutation pattern every other admin action in this file
already follows (`adjust_balance()`, `set_user_status()`,
`void_round_admin()`): acquire a pooled connection, open a transaction,
`SELECT ... FOR UPDATE` the current value, write the new one, record an
`admin_audit_log` row with full before/after JSON, admin id, reason, and
IP address. Wired to `POST /users/{user_id}/kyc` in `services/admin/
app.py`, gated by a new `users:verify_kyc` permission in `services/admin/
rbac.py` scoped to `{finance, superadmin}` -- the same pair as
`payments:approve`, not `users:suspend`'s `{ops, finance, superadmin}`,
because KYC level is a financial-compliance control (it gates withdrawal
size) rather than a user-standing one, even though both end up as a field
on the same `users` row. Promotions and demotions both go through this
same function and the same audit trail, so a level can be revoked (fraud
discovered, documents later found invalid) exactly as accountably as it
was granted.

**Deliberately not built**: any real document-collection or identity-
verification pipeline behind this action. `set_kyc_level()`'s docstring
is explicit that an admin is expected to have reviewed a user's identity
documents through some out-of-band channel before calling it -- which
channel that is (a manual support queue, a third-party KYC/eKYC
provider, something else) is a genuine, unmade product decision, not an
engineering one, and this turn deliberately scoped only the
engineering-judgment slice of the gap (a real, audited path *through*
the gate) rather than inventing a verification methodology no one has
actually chosen. `README.md`'s KYC gap description was updated to match:
the gap is now "no automated verification pipeline exists," not "no
writer exists at all."

**Verification**: three query-level tests
(`test_set_kyc_level_writes_audit_log_with_before_and_after`,
`test_set_kyc_level_can_also_revoke_a_previously_granted_level`,
`test_set_kyc_level_rejects_an_out_of_range_level`), two HTTP-level RBAC
tests (`test_rbac_support_cannot_set_kyc_level_over_http`,
`test_rbac_finance_can_set_kyc_level_over_http`), and one true end-to-end
test in `tests/integration/test_payments_withdrawals.py`
(`test_admin_kyc_promotion_unblocks_a_previously_rejected_withdrawal`)
proving the actual gap is closed: the same withdrawal request that raises
`KycLevelTooLow` before an admin promotes the user's level, via the real
`services.admin.queries.set_kyc_level()` call (not a raw SQL `UPDATE`
standing in for it), succeeds afterward. Full clean-slate rebuild:
`docker compose -f deploy/docker-compose.yml down -v` → `up -d` →
`alembic upgrade head` → `mypy` clean (63 source files) → full default
suite 739 passed (up from 733), 13 deselected → `-m load` one failure,
`test_load_multiroom.py` at 301.3ms against a 300ms budget, the same
well-documented shared-host-contention pattern seen repeatedly this
session (confirmed unrelated: no gateway/fanout code was touched) →
`-m chaos_infra` 1 passed → `-m e2e` 7 passed, fully clean.

---

## 2026-08-26 — `_settle_with_winners()` publishes winner balance updates concurrently

Two more catalogue findings closed this turn -- one investigated and found
to be a deliberate, already-optimized design, not a bug; the other real
and fixed.

**Investigated and found deliberate**: "hot-path redundant balance
re-query" -- the concern that `join()`/`drop_card()`/`_settle_with_
winners()` calling `ledger.publish_balance_update()` after their own
transaction commits re-reads balance data the same transaction already
had in hand. `user_balance_snapshot()`'s own docstring already documents
this as a prior code-review pass's deliberate fix (down from up to 9
round trips via `get_or_create_account()+balance()` × 3, to this one
query) specifically because it sits on the hottest path in the system --
every single stake. The fresh read happening on a new connection, after
commit, is also load-bearing, not incidental: it's the only way to
guarantee no concurrent transaction (a withdrawal, an admin adjustment,
the same user staking in a different room) mutated the balance in the
gap. Left alone.

**Fixed**: `_settle_with_winners()`'s own winner-balance-update loop was
a plain sequential `for user in winners: await publish_balance_update(...)`.
Each publish is fully independent -- a different user, its own pool
connection, its own Redis channel -- so a simultaneous-tie round with
several winners (`max_players` caps at 100) used to serialize several
round trips before `round_end` could even broadcast, delaying that
message for every player in the room, not just whichever winner's own
push was still waiting its turn. Made concurrent via `asyncio.gather`,
the same pattern already used elsewhere in this codebase for independent
per-item work (`services/gateway/queries.py`, `services/bot/
notification_relay.py`).

Added `test_settlement_publishes_winner_balance_updates_concurrently` to
`tests/integration/test_round_engine.py`, reusing the `bingo.winning_
patterns()` monkeypatch technique from the sibling tie-split test to make
two real, real-joined players deterministic simultaneous winners, plus a
second monkeypatch on `ledger.publish_balance_update()` itself giving one
winner's own publish an artificial 0.5s delay and recording real
timestamps -- directly proving the other winner's publish isn't
serialized behind it, not just asserting the loop "looks" concurrent.
Verified the regression test is real: `git stash push` on just `round_
engine.py` reverted to the old sequential code, reran -- failed with
`user_b's balance update landed 0.62s after settlement started`, matching
the sequential-stall prediction almost exactly -- then `git stash pop` to
restore the fix.

**Full clean-slate rebuild**: `mypy` clean (63 source files) → `pytest
tests/` (733 passed, up from 732) → `-m load`: the same already
-documented shared-host-contention pattern, unrelated to a change confined
to settlement's own balance-publish timing → `-m chaos_infra` (1 passed)
→ `-m e2e` (7/7 passed).

## 2026-08-26 — `recovery.py`'s crash-recovery sweep batched into two queries instead of `1 + N`

Two catalogue findings from the fresh `/code-review high` pass, closed the
same turn -- one turned out moot on investigation, the other real but
already fixed, and the third (this one) genuinely needed doing.

**Investigated and found moot**: "advisory-lock namespacing" (a concern
that `round_engine.py`'s per-user `pg_advisory_xact_lock` -- taken in
`join()` to close a daily-loss-cap TOCTOU race across a user joining two
rooms at once -- might now collide with a second, newer call site). A
full-codebase grep found exactly one real advisory-lock call site, same
as the comment already claims; `git log -S"pg_advisory_xact_lock"` shows
only the one commit that ever introduced it. No second call site exists,
so there is nothing for `user_id` to collide with.

**Investigated and found already fixed**: whether that same advisory lock
had any test coverage. It does --
`tests/integration/test_responsible_gaming.py`'s `test_loss_cap_holds_
under_two_concurrent_joins_in_different_rooms` fires two real concurrent
`engine.join()` calls (`asyncio.gather`) against two different rooms for
the same user with a cap deliberately set so either stake alone passes
but both together would exceed it, and asserts exactly one succeeds with
the loser's reason as `"loss_limit_reached"`. Confirmed it currently
passes.

**Actually fixed**: `recover_orphaned_rounds()` did one `SELECT max(seq)
...` round-trip per stuck round found, inside a loop -- a real N+1. This
function runs synchronously at `EngineWorker.start()`, before any room
can be claimed, so its own runtime directly delays the whole platform
coming back up after a real incident (many engines crashing together
would leave rounds stuck across many rooms at once, exactly the scenario
this function exists to recover from). Replaced with one batched query
(`GROUP BY room_id`) covering every room a stuck round belongs to,
scoped to just those room_ids via the same `room_id` column `ix_rounds_
room_status` already indexes -- not a full scan of every round this
platform has ever run. A pure refactor, not a behavior change: verified
by running the existing test suite unchanged rather than a revert-and
-confirm-it-fails step, since nothing was broken to begin with --
`test_stuck_round_is_still_recovered_after_the_room_gets_a_newer_round`
in particular already exercises the exact multi-round-per-room case this
refactor needs to keep handling correctly, and passed identically before
and after.

**Full clean-slate rebuild**: `mypy` clean (63 source files) → `pytest
tests/` (732 passed, same count -- a pure refactor) → `-m load`: the same
already-documented shared-host-contention pattern, unrelated to a change
that touches only a startup-time SQL query shape → `-m chaos_infra` (1
passed -- exercises `recovery.py` directly via a real crash-and-restart)
→ `-m e2e` (7/7 passed).

## 2026-08-26 — CI/CD: GitHub Actions, GHCR, and a self-hosted-runner deploy to Proxmox

Closes out the user's "do ci cd too" request. Two decisions genuinely
needed the user's own input rather than a guess, asked directly: where
this actually deploys (a self-hosted server on their own Proxmox box, not
a cloud platform), and how the deploy step updates it (build + push an
image to a registry, then pull + run it, rather than the simpler
`git pull` + rebuild-in-place alternative offered).

**`.github/workflows/ci.yml`**: `mypy --strict`, the default suite,
`-m chaos_infra`, and `-m e2e` all block merges; `-m load` runs too but
`continue-on-error: true` -- a GitHub-hosted runner's own CPU/network is
shared with whatever else is on that host at the time, the exact "clean
reading needs a dedicated, unshared process" reasoning `pyproject.toml`'s
own `load` marker docstring is already built around; an absolute latency
assertion on infrastructure like that would be noise, not a real signal,
the same conclusion this session's own dev-sandbox load-test flakiness
kept reinforcing throughout every other entry in this file that mentions
shared-host contention. A real `docker build` of the production image
runs too (not pushed anywhere from CI -- cd.yml's own job is what
publishes a real, deployable tag), so a packaging regression like the
`aiogram`/`httpx` one two entries back would be caught on the very next
push, not discovered by someone building it by hand months later.

**`.github/workflows/cd.yml`**: triggered by `workflow_run` watching CI,
not a plain `push` trigger -- there is no path from a red CI run to a
deploy, only from a genuinely green one on `main`. `build-and-push` runs
on a normal GitHub-hosted runner (needs real internet to reach GHCR) and
tags the image with both the commit SHA and `:latest`. `deploy` runs on a
**self-hosted runner** registered directly on the target server, since
GitHub's own cloud runners have no path to a private/local machine --
confirmed with the user this runner isn't set up yet; the workflow's own
header comment walks through registering one, plus the two setup steps a
workflow file genuinely can't do for the operator (creating
`deploy/.env` from the new `deploy/.env.prod.example` template on the
server itself, and optionally configuring a GitHub `production`
Environment with required reviewers -- flagged as worth turning on given
this is a real-money system, but left as the user's own call, not forced).
`clean: false` on the deploy job's checkout is what lets `deploy/.env`
survive every future deploy -- `actions/checkout`'s default behavior
would otherwise wipe it (an untracked file) on every single run.

**`deploy/docker-compose.prod.yml`**: Postgres, Redis, a one-shot
`migrate` service every real service `depends_on` with `condition:
service_completed_successfully` (so a fresh deploy can never race app
code against a schema it doesn't match yet), and all six deployable
units against the one image the Dockerfile builds -- YAML anchors
(`x-app-env`, `x-app-depends-on`) instead of repeating the same
`DATABASE_URL`/`REDIS_URL`/`depends_on` block six times. `JOBINGO_IMAGE`
is set by the CD workflow to the exact tag it just pushed; defaults to
`:latest` so the file is still directly runnable by hand. Validated for
real: `docker compose -f docker-compose.prod.yml config` against a throwaway
test `.env`, confirming every anchor merges and every `${...}` interpolates
exactly as intended -- not just written and assumed correct, the same
discipline as the Dockerfile and worker entrypoints before it. Both
workflow files also passed `actionlint` (a real static analyzer for GitHub
Actions YAML, downloaded and run directly, not just eyeballed) with zero
findings.

## 2026-08-26 — Metrics endpoints for the two workerless processes; a real bug in the shared test DB found by running the full suite against the fix

Verifying the Dockerfile's own build (previous entry) surfaced two more
real gaps, closed the same way: found by actually running things, not by
inspection.

**`packages/core/metrics.py`**: `services/engine/round_engine.py` and
`services/payments/payout_worker.py` already record real metrics
(`engine_calls_total`, `engine_rooms_active`, and others) against this
module's own default registry, but nothing ever served them anywhere in
production -- gateway/admin/payments/bot each define their own
framework-native `/metrics` route; the engine worker and payout worker are
plain background loops with no HTTP surface at all, and `deploy/
prometheus/prometheus.yml` had no scrape target for either. Added
`start_metrics_server(port)`, one small shared aiohttp app (not a second
web framework pulled in for one endpoint) rather than duplicating the
same handful of lines in both entrypoints. Wired into both `main()`s
(ports 8004/8005) and into `prometheus.yml`'s scrape config. Verified for
real: built the image, ran both processes against real dev Postgres/
Redis, `curl`ed both `/metrics` endpoints, got real Prometheus exposition
output back.

**A real, load-bearing bug in the shared test database**, found by running
the *full* suite (not just the directly-affected files) after adding
`test_run_active_rooms_is_safe_to_call_repeatedly` (previous session
entry): `redis.exceptions.MaxConnectionsError: Too many connections`,
elsewhere in the suite. Root cause: `rooms.is_active` defaults to `true`
(the schema's own default) and no test -- across dozens of tests, this
whole session -- ever set it back, so this session's shared dev database
had silently accumulated 3092 such rows. `run_active_rooms()` is the only
thing that ever queries that column in bulk, and no test exercised it
before this session's own auto-claim work added one; once it did, trying
to claim thousands of stray rooms at once was enough to genuinely exhaust
a real Redis client's connection pool during a full run, not just add
noise. Fixed at the source: `tests/integration/conftest.py`'s
`create_room()` now takes `is_active: bool = False`, overriding the
schema default, since an audit of every real production query against
that column (`admin/queries.py`'s dashboard count, `engine/worker.py`'s
scan, `gateway/queries.py`'s room list) confirmed no test needed the old
default *except* the handful actually exercising one of those three --
`test_dashboard_summary_reflects_real_state`, `test_run_active_rooms_is_
safe_to_call_repeatedly`, `test_full_gameplay_over_websocket`, and three
Playwright tests that browse the miniapp's own room list
(`test_miniapp_full_gameplay_flow`, `test_verify_draw_button_shows_a_
verified_seed`, `test_history_tab_shows_a_completed_round`) -- each
updated to pass `is_active=True` explicitly. Deactivated the existing
3092+ accumulated rows the same way as before (safe, reversible, scoped
to this session's own `test-room-%`/`admin-test-%` naming patterns); a
full suite run afterward left only 16 active rooms, confirming the fix
actually bounds the accumulation rather than just resetting it once.

## 2026-08-26 — Dockerfile, and two real packaging bugs it caught immediately

CI/CD step two: a real `docker build` of this project, verified by actually
building and running it -- not just writing a Dockerfile and assuming.

**`Dockerfile`**: one image for all six deployable units (each gets its own
`command:` in `deploy/docker-compose.prod.yml`, added next). Editable
install (`pip install -e .`), matching this project's own documented local
-dev method exactly, deliberately not a "real" wheel build:
`services/gateway/app.py` locates `web/miniapp/` by a path relative to its
own file location, which only survives if the source tree stays laid out
exactly as it is in the repo -- an editable install (a `.pth` file
pointing straight back at `/app`) preserves that; installing into
site-packages would not. No `build-essential`: confirmed directly (a real
build, not assumed) that every C-extension dependency here -- asyncpg,
cryptography, bcrypt, psycopg2-binary -- installs from a prebuilt
manylinux wheel against this exact base image.

**Building it immediately surfaced two real, pre-existing packaging bugs**,
invisible until now because every dev/test environment this whole project
has been built in already had every dependency installed, tracked or not:

- `aiogram` was never declared in `pyproject.toml` at all -- not in the
  base dependencies, not in `[dev]`. A plain `pip install .` (this
  Dockerfile's own install step) would produce an image with no bot
  functionality whatsoever, failing at import time. `aiohttp` (aiogram's
  own transport, also imported directly by `services/bot/app.py`) was
  equally undeclared, riding along only because *something* had
  installed it manually at some earlier point in this project's history.
- `httpx` -- `services/payments/chapa.py`'s real HTTP client for calling
  Chapa's API -- was declared only under the `[dev]` extra, alongside
  pytest/mypy/playwright. A production install skipping dev extras
  (correctly, since none of those belong in a production image) would
  have shipped with no way to actually reach a payment provider.

Fixed by moving `aiogram`, `aiohttp`, and `httpx` into `pyproject.toml`'s
base `dependencies`, matching the versions already proven working in
every dev environment this session has used.

**Verification**: sandbox containers here can't resolve DNS (a sandbox
-specific Docker networking limitation -- raw IP connectivity works fine,
confirmed via `ping 8.8.8.8`; the host's own `pip` reaches PyPI directly
with no issue), so every `docker build`/`docker run` in this entry used
`--network=host` to borrow the host's working resolver -- a local
-verification-only workaround, not something baked into the Dockerfile or
any workflow; GitHub's own runners and the target Proxmox server both
have normal networking. Built the image, then actually ran all six
services against this project's real dev Postgres/Redis: `uvicorn` for
gateway/admin/payments (each confirmed via its own startup log), and
`python -m services.X` for bot/engine-worker/payout-worker -- the bot
confirmed via a real `curl` to `/healthz` returning `{"status": "ok"}`,
the other two confirmed via clean startup logs.

## 2026-08-26 — Real production entrypoints for the engine worker, payout worker, and bot

User request ("do ci cd too"), first step: CI/CD needs something real to
build and run. Before this, three of this repo's six deployable units had
no way to actually start as a long-running process -- `services/engine/
worker.py`'s `EngineWorker` and `services/payments/payout_worker.py`'s
`run_forever()` were both classes/functions with no `main()` anywhere,
and `services/bot/app.py` built an aiohttp `Application` but never
actually served it, registered the webhook with Telegram, or started the
`Notifier`/`notification_relay.py` pipeline. The other three (gateway,
admin, payments) are already real FastAPI apps startable via a plain
`uvicorn services.X.app:app`, so they needed no changes.

**`services/bot/app.py`**: added `main()` -- builds the bot, pool, redis,
`Notifier` (started), and `notification_relay.run_forever()` as a
background task sharing that one `Notifier` instance (its own docstring:
this is what keeps the global rate pace and per-chat 429 backoff enforced
in exactly one place, rather than needing a second process to
coordinate with this one), registers the webhook with Telegram via
`bot.set_webhook()` if `public_base_url` is configured, and serves via
`aiohttp.web.run_app()` (which already handles SIGTERM/SIGINT and drives
`on_shutdown`, no manual signal wiring needed).

**`services/engine/worker.py`**: added `main()` -- crash recovery, claims
every active room, then re-scans on a 30s timer so a room activated
*after* startup still gets an engine (nothing previously watched for
that at all). This exposed a real, independent bug in `run_active_rooms()`
itself: it called `claim_room()` unconditionally for every active room on
every invocation, and `claim_room()` has no guard against being called
twice for a room it already owns -- it just overwrites `self._engines`/
`self._tasks`, silently orphaning the previous, still-running engine task
(no reference left to stop it on shutdown) while a redundant second
engine raced it for a lock it could only lose. Fixed by skipping any
room whose task is already running. Added
`test_run_active_rooms_is_safe_to_call_repeatedly` to `tests/integration/
test_worker.py`. Verified it against the unfixed code via `git stash`:
it failed, but not with a clean assertion -- with a raw `redis.exceptions
.ConnectionError` from a *different* test's teardown, because the
orphaned engine task from the first `run_active_rooms()` call was still
alive in the background, still polling Redis for its lock refresh, when
pytest closed the shared `redis` fixture's connection -- an even more
concrete demonstration of the bug than a bare assertion would have been.

Writing this test surfaced a separate, pre-existing data-hygiene problem:
`rooms.is_active` defaults to `true` (schema default) and no test ever
deactivates a room afterward, and this session's shared dev database has
accumulated 3092 such rows across months of testing. `run_active_rooms()`
is the first thing to ever run an unscoped `WHERE is_active = true` query
and act on every row -- every prior test either calls `claim_room()`
directly by id or bypasses `EngineWorker` entirely. Confirmed no test
asserts an absolute `is_active` count (`test_dashboard_summary_reflects_
real_state` uses `>= 1`, matching this codebase's established
"deltas/bounds, not absolutes, against a shared accumulating database"
convention) and no round was left non-terminal, then deactivated all
`test-room-%`/`admin-test-%` rows (3092 total) -- safe, reversible, and
scoped precisely to this session's own test-data naming patterns.

**`services/payments/payout_worker.py`**: added `main()` -- the payout
stream consumer (`run_forever()`, the primary job) alongside the two
other "safe to run on a timer" payments sweeps that had no periodic
invoker anywhere: `deposits.py`'s `poll_pending_deposits()` (a webhook
that never arrives) and `withdrawals.py`'s `sweep_stuck_approved_payouts()`
(an enqueue that never landed). All three share one process/`ChapaProvider`
rather than three separate containers, since none individually justifies
its own; each sweep's own exception is caught and logged per-tick rather
than killing the other two, the same isolation reasoning as `_handle_
command()`'s and the auto-claim scan's own fixes.

**Full clean-slate rebuild**: `mypy` clean (63 source files) → `pytest
tests/` (732 passed, up from 731) → `-m load`: the same already
-documented shared-host-contention pattern, worse again (492-667ms against
the 300ms budget, load average 2.02, climbing across this session purely
from other unrelated Docker projects on this shared host) -- confirmed
this batch's new code can't be the cause, since every new `main()`/
`main_async()` is gated behind `if __name__ == "__main__":` and never
invoked by any test → `-m chaos_infra` (1 passed) → `-m e2e` (7/7 passed
clean).

## 2026-08-26 — Auto-claim scan no longer crashes the whole room on an unexpected exception

Tenth fix from the fresh `/code-review high` pass, revisited on request.
Turned out more severe than the finding's own framing suggested.

**The bug**: `_call_next_number()`'s auto-claim scan (`round_engine.py`)
looped over every `auto_mark`-enabled entry and called `self.claim(user_id,
source="auto")` for anyone with a winning pattern, with no exception
handling around that call at all -- unlike `_handle_command()`, the
manual/gateway command path, which already wraps every command in `try
/except Exception:` for exactly this reason ("One bad command must fail
that command, not the room"). An unexpected exception from `claim()` --
realistically its own `_record_claim_attempt()` audit-log write, the one
real DB call `claim()` leaves unguarded -- didn't just skip *later*
entries in the same scan: it propagated straight through
`_call_next_number()`, `_run_running()`'s bare `for` loop, and
`run_forever()`'s own `while` loop, killing the room's entire engine
`asyncio.Task`. Nothing restarts it. The round sits stuck until a
*different* engine worker starts and `recovery.py`'s crash sweep finds it
-- which **voids and refunds** the round rather than resuming it, so the
legitimate winner loses their win entirely, and every other player in the
room loses their round to a refund, over one exception.

**Fixed**: wrapped the `claim()` call in `try/except Exception:`,
matching `_handle_command()`'s own established pattern and logging via
`logger.exception()` the same way. On failure, also removes `user_id`
from `_auto_claimed` so the *next* number call retries them: `claim()`
raising means it never reached its own state-mutating section (that
happens well after the one DB write that can actually fail, per its own
control flow), so nothing about the round was left inconsistent -- the
user's winning pattern is exactly as valid on the next call as it was on
this one, and bingo patterns only ever gain numbers, never lose them.

Added `test_an_unexpected_exception_during_auto_claim_does_not_crash_the_
room` to `tests/integration/test_round_engine.py`, reusing the
`bingo.winning_patterns()` monkeypatch technique from the sibling
`test_two_simultaneous_auto_mark_winners_both_split_derash` test to make
exactly one real, real-joined player's card a deterministic immediate
winner -- no reliance on the real card pool's draw-order luck to land a
specific player's win on a specific call. Verified the regression test is
real: `git stash push` on just `round_engine.py` reverted to the old,
unguarded code, reran -- failed with the `RuntimeError` propagating all
the way through `run_forever()` and killing the engine task, exactly as
the bug describes -- then `git stash pop` to restore the fix.

**Full clean-slate rebuild**: `mypy` clean (63 source files) → `pytest
tests/` (731 passed, up from 730) → `-m load`: the same already
-documented shared-host-contention pattern from the two prior entries,
worse at first (3 failures, load average 2.51 on this 4-core host,
confirmed via `docker ps` showing `spos-backend` restarting again) then
narrowing to 1 as load settled to 1.22 -- never a test related to this
fix, which touches only exception handling around an already-existing
`claim()` call with zero behavioral change on the success path (a
try/except wrapping identical code is a no-op when nothing raises) → `-m
chaos_infra` (1 passed) → `-m e2e`: `test_miniapp_full_gameplay_flow`
flaked three different ways across repeated full-batch runs under the
same elevated load (a balance mismatch, a hidden `#your-card-section`,
a `#screen-game.active` timeout) while passing clean every time it ran in
isolation or once load dropped -- consistent with this session's already
-established "varying, non-reproducible Playwright symptoms under host
load, always clean on rerun" pattern, not a regression: nothing about
spectator-mode or room-capacity logic is anywhere near this fix. A later
full `-m e2e` run, once load had settled, passed 7/7 clean.

## 2026-08-26 — Gave `test_full_round_35_players_ledger_balances` real lobby margin

A follow-up, not from the review catalogue: surfaced incidentally while
verifying the previous fix, and worth closing immediately since a flaky
test undermines the "a green suite means safe" discipline this whole
session runs on.

**The bug**: `create_room()`'s `lobby_seconds` defaults to 1 -- fine for
every other test in this file, which either join a handful of players or
join many *concurrently* via `asyncio.gather`. This test joins 35 users
*sequentially*, each a real, multi-round-trip `engine.join()` call, and
`round_engine.py`'s own `_lobby_deadline_monotonic` is fixed the instant
the first join starts the round -- it is never extended by later joins.
Confirmed directly, not assumed: with the old 1-second default, this test
failed 3 of 5 runs with `not_joinable` (a later `join()` call arriving
after the lobby had already closed) purely from real host contention
(other, unrelated Docker containers competing for this shared 4-core
box) -- no code defect, just no margin at all for 35 sequential real
transactions once the host is under any load.

**Fixed**: `lobby_seconds=20` for this test only (every other test in
this file keeps its own, already-correct value). Confirmed 5/5 clean
runs at ~21.5s each. Raised the test's own subsequent `wait_until(...,
timeout=15)` to `45` to match -- the round can't even start running
until the longer lobby window elapses.

**Full clean-slate rebuild**: `mypy` clean (63 source files, unaffected)
→ `pytest tests/` (730 passed, same count -- a timing-only change to an
existing test) → `-m load`: same already-documented shared-host
-contention flake as the previous entry, this time on `test_gateway_
fanout.py` (`test_stalled_reader_does_not_delay_other_sockets` in the
full batch, `test_many_sockets_receive_a_call_within_budget` on an
isolated rerun, 377-451ms against the 300ms budget) -- a different
subsystem (WebSocket fanout, not round-engine lobby timing) than
anything touched here, confirming this is ambient host load rather than
anything connected to this change → `-m chaos_infra` (1 passed) → `-m
e2e` (7/7 passed clean).

## 2026-08-26 — `ledger_transactions_total` no longer overcounts across a caller's own rollback

Ninth fix from the fresh `/code-review high` pass -- the largest and most
architecturally significant one, revisited on request after the session's
earlier status summary had described the review catalogue's remaining
items as lower-priority.

**The bug**: an earlier fix this session made `packages/core/ledger.py`'s
`post()` increment `ledger_transactions_total` right after its own `async
with conn.transaction()` block exited, reasoning that this only fires
"after the transaction has actually committed." That's true of `post()`'s
own block in isolation, but false for what actually matters: every real
production caller (10 call sites across 6 files) already has its own
transaction open by the time it calls `post()`, which makes `post()`'s
block a Postgres `SAVEPOINT`, not a real `COMMIT`. If a *later* statement
in the caller's own transaction then failed -- an `UPDATE`, an audit-log
`INSERT`, any of the real work several of these call sites do right after
posting -- the whole thing rolled back, but the metric had already fired
for a ledger write that never actually persisted. `git blame` confirmed
this was introduced by this session's own earlier fix, not pre-existing.

**Fixed**: `post()` now checks `conn.is_in_transaction()` *before* opening
its own block. If the caller already had one open, `post()` can't know
whether it will ultimately commit, so it leaves the metric to the caller
-- the same convention this file's own `publish_balance_update()` already
documents for the identical reason. Only when `post()` itself is the real,
non-nested transaction owner (this module's own tests, notably) does it
record the metric internally.

Every real call site was updated to record the metric itself, right after
its *own* transaction genuinely commits:
`round_engine.py`'s `join()`, `drop_card()`, `_settle_with_winners()`;
`payout_worker.py`'s `_settle_success()`, `_reverse()`;
`withdrawals.py`'s `request_withdrawal()`; `deposits.py`'s webhook handler
(matching its own already-established `deposit_outcomes_total` placement
exactly); `admin/queries.py`'s `adjust_balance()`, `reject_withdrawal_
admin()`. `refunds.py`'s `refund_round_in_transaction()` -- called from
inside *either* of two different callers' transactions (`refund_round()`'s
own, or the admin void action's) -- can't safely record it either, for
the same reason `post()` can't; it now returns the number of entrants
refunded (0 for a no-op) instead of a bare bool, so each of its two real
owners can record the right count once their own transaction commits.
`refund_round()` itself always owns a real, non-nested transaction (a
fresh `pool.acquire()`), so it records the metric internally, the same
way `post()` does when non-nested -- its own three callers
(`round_engine.py` x2, `recovery.py`) needed no changes at all.
`void_round_admin()` calls `refund_round_in_transaction()` directly (not
through `refund_round()`), so it records the metric itself; its own
external contract (a strict `bool`, checked with `is True`/`is False` in
an existing test, and returned as JSON from `/rounds/{id}/void`) was
kept byte-for-byte identical by converting the internal count with
`bool(...)` at the return statement, rather than changing its signature.

Added four tests. `tests/integration/test_metrics.py`'s `test_post_does_
not_increment_the_metric_when_the_outer_transaction_rolls_back` is the
core regression: calls `post()` with `conn` already inside a manually
-started transaction, then rolls that outer transaction back, and asserts
the counter never moved. `test_join_increments_ledger_transactions_
metric_on_a_real_commit` confirms the other side -- a real call site still
increments correctly once responsibility actually moved to it. Extended
the existing `test_rounds_voided_counter_increments_on_a_real_refund` to
also assert `ledger_transactions_total{kind="refund"}` increases by
exactly the entrant count (2), not just once regardless of how many --
this caught a real design flaw in an earlier draft of this fix (see
below). `tests/integration/test_admin_queries.py`'s existing `test_void_
round_admin_refunds_and_is_idempotent` (`is True`/`is False` checks) and
`services/admin/app.py`'s `/rounds/{id}/void` JSON response were the
concrete reason `void_round_admin()`'s own return type stayed `bool`
rather than becoming `int` like the two internal functions underneath it.

Verified the regression test is real: `git stash push` on just `ledger.py`
reverted to the old, unconditional-increment code, reran -- failed with
`assert 1.0 == 0.0` ("metric incremented even though the outer
transaction rolled back") -- then `git stash pop` to restore the fix.

**A design flaw caught by the test suite itself, not by inspection**: the
first draft of this fix left `refund_round()`'s three callers
(`round_engine.py` x2, `recovery.py`) responsible for incrementing the
metric themselves using the count `refund_round()` returned. Running the
extended `test_rounds_voided_counter_increments_on_a_real_refund` against
that draft failed -- `1.0 == (1.0 + 2)`, no increment at all -- because
the test calls `refunds.refund_round()` *directly*, the same way a real
caller would, and none of those three real callers' own wrapper code was
what the test was exercising. This is exactly what the fix's own
reasoning says should have happened: `refund_round()` always owns a real,
non-nested transaction, so pushing the recording responsibility out to
*its own* callers was unnecessary indirection, not the caller-can't-know
-if-it-committed problem the rest of this fix addresses. Moved the
increment back into `refund_round()` itself and reverted the three
now-redundant caller-side additions -- simpler, and the actual bug this
test exists to catch (a caller of `refund_round()` forgetting the
increment) is structurally impossible now rather than merely unlikely.

**Full clean-slate rebuild**: `mypy` clean (63 source files) → `pytest
tests/` (730 passed, up from 728) → `-m load`: `test_load_multiroom.py`
and `test_gateway_fanout.py` intermittently exceeded their 300ms p99
budgets across six full-batch attempts (304-576ms), never on a test
related to this fix; confirmed via a controlled experiment (`git stash`
on just `round_engine.py`, 5 trials each way) that `test_full_round_
35_players_ledger_balances` -- a different, timing-tight test that
surfaced during this same verification pass -- fails at the *same* rate
(3/5) with this fix fully reverted as with it applied, proving that
specific flake pre-exists this change entirely; `docker ps` showed
`spos-backend` actively restarting and load average at 1.94 on this
4-core host, both confirmed twice. `test_load_multiroom.py` alone (no
other test running) still exceeded budget (447ms) on a dedicated run,
confirming this is genuine current host contention from unrelated
projects, not a contention effect between tests in the same batch, and
architecturally unconnected to anything this fix touches (ledger
-transaction metric recording, not the gateway WebSocket fanout path
these two tests exercise) → `-m chaos_infra` (1 passed) → `-m e2e`: one
transient failure on `test_miniapp_full_gameplay_flow` (a Playwright
selector-visibility flake under the same host load, different symptom
each occurrence -- an already-documented pattern from earlier in this
session), clean on immediate rerun and on a full 7/7 batch rerun.

## 2026-08-25 — Restored a dropped assertion in `test_small_amount_auto_approved_and_enqueued`

Eighth and last straightforward item from the same fresh `/code-review
high` pass; the remaining catalogue is lower-priority/more speculative
findings, left for a future pass rather than worked mechanically now.

**The gap**: this test only checked that funds left `user_cash`
(`_cash(conn, user_id) == Decimal("900.00")`) after a small, auto-approved
withdrawal, never that they actually landed in `user_locked` -- the sibling
test for the review path (`test_amount_above_auto_approve_limit_goes_to_
review`) already checks exactly that (`_locked == 3000.00`, with its own
comment: "funds are still locked immediately, review or not -- only the
payout dispatch is deferred, never the fund lock"), but the auto-approved
path's own test never carried the matching assertion. Without it, a bug
that made funds vanish, double-deduct, or land somewhere other than
`user_locked` on this specific path (small-amount, auto-approved) would
have passed this test silently.

**Fixed**: added `assert await _locked(conn, user_id) == Decimal("100.00")`,
matching the sibling test's pattern exactly.

Test-only change with no corresponding production bug to reproduce, so the
usual "revert and confirm failure" step doesn't apply the same way here --
confirmed instead that the new assertion passes against the real,
already-correct `request_withdrawal()` behavior.

**Full clean-slate rebuild**: `mypy` clean (63 source files) → `pytest
tests/` (728 passed, same count -- an assertion added to an existing test,
not a new one) → `-m load` (5/5 passed in isolation; the full-batch run hit
the same already-documented shared-host-contention flake in
`test_gateway_fanout.py::test_stalled_reader_does_not_delay_other_sockets`,
unrelated to this change) → `-m chaos_infra` (1 passed) → `-m e2e`: the
first full-batch run hit two failures
(`test_miniapp_full_gameplay_flow`, `test_history_tab_shows_a_completed_
round`), both the same `#screen-game.active` selector timeout -- traced to
running the e2e batch immediately after chaos_infra's own deliberate Redis
restart (confirmed via `docker ps` showing `jobingo-redis-1` "Up 2
minutes"), compounding the already-known shared-host contention rather
than a real regression from a test-only assertion addition unrelated to
the miniapp. Reran the full `-m e2e` batch once more, further removed from
that restart: 7/7 passed clean. 741/741 real passes.

## 2026-08-25 — `retention_cohorts` now buckets weeks using Ethiopia time, not UTC

Seventh fix from the same fresh `/code-review high` pass.

**The bug**: `services/admin/queries.py`'s `retention_cohorts()` computed
`cohort_week` and `active_week` via `date_trunc('week', created_at)::date`
and `date_trunc('week', r.started_at)::date` -- both operating on a
`timestamptz` with no `AT TIME ZONE`, which truncates using Postgres's
ambient session timezone (unconfigured, defaults to UTC). This is the same
bug an earlier fix this session already closed twice in this same file
(`dashboard_summary`'s day cutoff, `daily_ggr`'s two day cutoffs) --
DECISIONS.md's existing entry for that fix explains it in full -- just via
`date_trunc('week', ...)` instead of a bare `::date` cast, and never
applied here. A signup or round in the ~3-hour window where UTC still says
"Sunday" but Ethiopia (UTC+3) already says "Monday" got bucketed into the
wrong cohort week entirely -- not just off by a few hours like the
day-level version of this bug, but placed in an adjacent week's row,
changing that week's `cohort_size` and every `active_users` count derived
from it.

**Fixed**: both `date_trunc('week', ...)` calls now wrap their `timestamptz`
argument in `... AT TIME ZONE 'Africa/Addis_Ababa'` first, exactly matching
the established pattern from the other two sites in this file.

Added `test_retention_cohorts_buckets_signup_week_using_ethiopia_time_not_utc`
to `tests/integration/test_admin_queries.py`, pinning a signup to
`2024-03-10 22:00:00 UTC` -- confirmed directly against this project's own
Postgres (not assumed) to truncate to `2024-03-04` without the fix and
`2024-03-11` with it. Picked a date safely in the past (this session's
"now" is 2026-08-25) so no unrelated test's `create_funded_user()` call
ever lands in either of these same two week buckets and pollutes the
count -- confirmed empirically during the regression-test verification
step below, where the only other cohort weeks present were unrelated
2026 dates. Also updated the existing
`test_retention_cohorts_places_a_backdated_signup_in_a_later_week_offset`'s
own verification query to the same `AT TIME ZONE` pattern -- its bare
`date_trunc('week', created_at)::date` would have silently drifted out of
sync with the now-fixed function and turned flaky near a future UTC/EAT
week-boundary crossing, rather than failing loudly.

Verified the new test is real: `git stash push` on just `queries.py`
reverted to the old, UTC-based code, reran -- failed with `expected the
Ethiopia-time Monday (2024-03-11), got cohort weeks {'2026-08-24',
'2026-08-10', '2024-03-04'}` (the old buggy bucket, plus two unrelated real
cohort weeks from this session's own test data, confirming the
date-collision-safety assumption held) -- then `git stash pop` to restore
the fix.

**Full clean-slate rebuild**: `mypy` clean (63 source files) → `pytest
tests/` (728 passed, up from 727) → `-m load` (5/5 passed clean) → `-m
chaos_infra` (1 passed) → `-m e2e` (6/7 passed on the first pass,
`test_history_tab_shows_a_completed_round` hit the already-documented
occasional Playwright flake with a 25s selector timeout unrelated to this
change; reran clean in isolation, then the full `-m e2e` batch reran
7/7 clean). 741/741 real passes.

## 2026-08-25 — `/auth/login` now enforces the IP allowlist

Sixth fix from the same fresh `/code-review high` pass.

**The bug**: `services/admin/app.py`'s `_check_ip_allowlist()` is enforced
in exactly two places -- inside `current_admin()`, the session dependency
almost every admin route goes through via `Depends(require(...))`, and
directly inside `/metrics` (the one other route with no session, since a
Prometheus scraper can't present a bearer token -- itself a fix from an
earlier pass this session). `/auth/login` never went through either path:
it takes no bearer token by definition (that's what it's issuing), so it
never called `current_admin()`, and nobody had added a direct call the way
`/metrics` got one. It's actually the single most exposed route to have
missed this check on -- every other admin route was already unreachable to
an IP outside the allowlist, but that same attacker could still throw
password/TOTP guesses directly at `/auth/login`.

**Fixed**: added `request: Request` to the handler's signature and a
`_check_ip_allowlist(request)` call as the first line, matching the exact
pattern already used by `/metrics`.

Added `test_login_endpoint_is_blocked_by_the_ip_allowlist` to
`tests/integration/test_admin_app.py`, mirroring the existing
`test_metrics_endpoint_is_blocked_by_the_ip_allowlist` pattern (set
`admin_app.state.ip_allowlist`, hit the route over real HTTP, assert
`403`, restore in `finally`). Verified the regression test is real: `git
stash push` on just `app.py` reverted to the old code, reran -- failed
cleanly with `assert 200 == 403` (login succeeded despite the IP not being
on the allowlist) -- then `git stash pop` to restore the fix.

**Full clean-slate rebuild**: `mypy` clean (63 source files) → `pytest
tests/` (727 passed, up from 726) → `-m load` (5/5 passed clean this run)
→ `-m chaos_infra` (1 passed) → `-m e2e` (7 passed). 740/740.

## 2026-08-25 — `room_lock.py` now tolerates one transient Redis error instead of relinquishing immediately

Fifth fix from the same fresh `/code-review high` pass. High-stakes area
(game-engine room ownership), so treated with extra care rather than
applied mechanically.

**The bug**: an earlier fix this session made `_refresh_loop()` relinquish
ownership (`self._held = False`, stop looping) on *any* exception from the
refresh `eval()` call, to close a genuine split-brain risk (an unhandled
error used to kill the task before `self._held = False` ran, so `is_held()`
reported `True` forever even after the real key expired and a second
engine legitimately took over). That fix over-corrected: a single
transient blip -- one bad round-trip, nowhere near the actual TTL --
now made a perfectly healthy engine voluntarily abandon a room it still
legitimately owns. Worse, a failed `eval()` never touches the Redis key
itself (only `_DELETE_IF_OWNER` does that), so the key survives with its
original owner and TTL -- meaning no *other* engine could take over either,
since `SET NX` would still see it occupied. Every player in that room
would stall for up to `ttl_seconds` over something that would have cleared
by the very next scheduled refresh 5 seconds later.

**Fixed**: track `self._last_refreshed_at` (the event loop's monotonic
clock), set at `acquire()` and after every successful refresh. On a
refresh exception, only relinquish once `elapsed >= ttl_seconds -
refresh_interval_seconds` -- one full refresh interval of safety margin
below the actual TTL, so by the time this gives up, the real Redis-side
key (barring clock skew) still has that much time left. Below that margin,
log and retry on the next scheduled interval instead. The `if not
refreshed:` branch (a *successful* eval reporting someone else now owns
the key) is untouched -- that's a definitive signal, not something to
retry, and was never the problem.

Rewrote `tests/integration/test_room_lock.py`'s existing
`test_a_redis_error_during_refresh_relinquishes_ownership_rather_than_sticking`
into `test_sustained_redis_errors_during_refresh_eventually_relinquish_
ownership` (its single-failure premise no longer holds under the new
behavior by design; sustained failure past the safety margin still
relinquishes, preserving the original split-brain property it was written
to protect) and added
`test_a_single_transient_redis_error_during_refresh_does_not_relinquish_
ownership` for the actual bug being fixed here. Verified the new test is
real: `git stash push` on just `room_lock.py` reverted to the old,
immediate-relinquish code, reran -- failed cleanly with `assert False is
True` (old code relinquished on the first blip, exactly as expected) --
then `git stash pop` to restore the fix.

**Full clean-slate rebuild**: `mypy` clean (63 source files) → `pytest
tests/` (726 passed, up from 725) → `-m load` (5/5 passed in isolation;
the full-batch run hit the same already-documented shared-host-contention
flake in `test_gateway_fanout.py::test_stalled_reader_does_not_delay_
other_sockets`, ~397ms against a 300ms budget -- unrelated to this
change, confirmed via the same `docker ps` contention pattern seen on the
prior fix this session) → `-m chaos_infra` (1 passed -- this test
restarts Redis mid-round and exercises `RoomLock`'s real refresh-failure
path directly) → `-m e2e` (7 passed). 739/739 real passes.

## 2026-08-25 — Fixed `waitForAuth()` hanging forever on a terminal auth failure

Fourth fix from the same fresh `/code-review high` pass.

**The bug**: `web/miniapp/js/ws.js`'s `waitForAuth()` resolves only via the
"authed" message handler draining `authResolvers`. When the WebSocket
closes with one of `_TERMINAL_CLOSE_CODES` (4000/4001/4003 -- a handshake
that can only ever fail again, most commonly stale `initData` past
Telegram's own validity window), the code correctly set
`connection: "auth_failed"` but never touched `authResolvers` at all --
"authed" is the only other place a resolver is ever drained, and that
message is never coming for a connection that just failed terminally.
`app.js`'s `boot()` does `const user = await ws.waitForAuth();` directly on
first load, so this hung the entire Mini App forever instead of ever
progressing past the loading state -- even though the connection-state
`subscribe()` banner (added by an earlier fix) already shows "reload the
app" correctly, entirely independent of this stuck promise. A second,
related gap: calling `waitForAuth()` *after* the terminal failure already
happened (state already `"auth_failed"`) would queue a new resolver that
also could never fire, since nothing re-sends "authed" for a dead
connection.

**Fixed**: `authResolvers` now stores `{resolve, reject}` pairs. The
terminal-close branch drains and rejects every pending one. `waitForAuth()`
itself now also checks for `connection === "auth_failed"` up front and
rejects immediately, matching its existing `"connected"` fast-path.
`app.js`'s `boot()` wraps the `await` in try/catch and simply returns on
rejection -- the reload banner is already live via the state subscriber,
so there's nothing further for `boot()` itself to do.

Added `tests/frontend/test_wait_for_auth_terminal_failure.mjs` (run via
`tests/unit/test_miniapp_wait_for_auth_terminal_failure.py`, matching this
repo's existing plain-node-script pattern for testing the framework-free
Mini App JS -- no existing test touched `waitForAuth`/`auth_failed` at
all). Stubs the minimum browser globals (`window.location`, `WebSocket`)
`ws.js` needs, drives a fake socket's `close` event with a real terminal
code (4003), and asserts the pending `waitForAuth()` promise rejects
rather than hanging -- and that a second call made after the failure
rejects immediately too. Added an internal 2-second deadline
(`DeadlineExceeded`, explicitly distinguished from a real rejection in the
`catch` blocks) so a future regression fails fast with a clear assertion
instead of hanging the whole node subprocess until pytest's own 30s kill.

Verified the regression test is real: `git stash push` on just `ws.js`
reverted to the old, buggy code, reran -- failed exactly as expected,
`DeadlineExceeded: waitForAuth() after a terminal close: did not settle
within 2000ms`, in 2.09s rather than a slow 30s subprocess timeout -- then
`git stash pop` to restore the fix.

**Full clean-slate rebuild**: `mypy` clean (63 source files, unaffected by
this frontend-only change) → `pytest tests/` (725 passed, up from 724) →
`-m load` (5/5 passed in isolation; the full-batch run hit the same
already-documented shared-host-contention flake in
`test_gateway_fanout.py::test_stalled_reader_does_not_delay_other_sockets`,
409ms/399ms against a 300ms budget, confirmed via `docker ps` showing three
other unrelated projects' containers competing for this 4-core host --
unrelated to this change, which touched no Python) → `-m chaos_infra` (1
passed) → `-m e2e` (7 passed, including the real-browser `boot()` golden
path). 738/738 real passes.

## 2026-08-25 — Fixed referral credit silently dropped for Mini-App-first users

Third fix from the same fresh `/code-review high` pass, again independently
caught by two separate finder agents.

**The bug**: `services/gateway/queries.py`'s
`get_or_create_user_by_telegram_id()` lazily creates a phoneless `users` row
for anyone who opens the Mini App before ever messaging the bot. When that
person later shares their contact to actually register,
`register_from_contact()` (`services/bot/registration.py`) routes them
through `_attach_phone_to_existing_user()` instead of the `INSERT` path --
and that function only ever wrote the two phone columns, never
`referred_by`. Meanwhile `handlers.py`'s `on_contact()` unconditionally
cleared the pending-referral Redis key on any non-exception return,
with no check that `referred_by` was actually persisted. Net effect: a
Mini-App-first user who clicked a referral link and then registered lost
the referral permanently, silently, with no error anywhere -- the
referrer's own dashboard would just never show that signup. `on_contact()`'s
own comment already documented the intended invariant ("Only cleared once
registration has actually recorded it in users.referred_by") from an
earlier fix, but the invariant wasn't actually true for this path.

**Fixed**: resolved `referred_by_id` once, up front in
`register_from_contact()`, and reused it in all three places a referral can
end up recorded -- the original `INSERT`, and both of
`_attach_phone_to_existing_user()`'s call sites (the plain existing-phoneless
-row branch, and the `UniqueViolationError`-retry branch for a concurrent
`telegram_id` race). `_attach_phone_to_existing_user()` now takes
`referred_by_id` and writes `referred_by = COALESCE(referred_by, $4)` --
never overwriting an already-set value, though in practice this row's
`referred_by` should always be `NULL` the first time it reaches this
function. A referral only fails to record now if `referred_by_telegram_id`
itself doesn't resolve to a real user (a dead/invalid referral code) --
already-correct, pre-existing behavior (`test_unknown_referrer_is_ignored_
not_an_error`), unrelated to this bug.

Added `test_register_from_contact_records_referral_for_a_phoneless_row` to
`tests/integration/test_registration.py`. Verified it's a real regression
test the standard way: `git stash push` on just `registration.py` reverted
to the old, buggy code, reran -- failed with `assert None == 6829` (the
referrer's real DB id, expected but not found) -- then `git stash pop` to
restore the fix.

**Full clean-slate rebuild**: `mypy` clean (63 source files) → `pytest
tests/` (724 passed, up from 723) → `-m load` (5 passed) → `-m chaos_infra`
(1 passed) → `-m e2e` (7 passed). 737/737 total.

## 2026-08-25 — Fixed `notification_relay.py`'s head-of-line blocking across users

Second fix from the same fresh `/code-review high` pass, again independently
caught by two separate finder agents.

**The bug**: `run_forever()`'s batch loop was `for msg_id, fields in
entries: await process_one(...)`, and `process_one()` awaits
`notifier.send()`'s returned future all the way to a terminal outcome --
which for a chat currently in a Telegram 429 backoff can mean several
`Notifier._run()` retry/sleep cycles (`services/bot/notifier.py`, up to
`MAX_BACKOFF_SLEEP_SECONDS` per cycle, repeated until the chat's own
`retry_after` window clears). One backed-off chat_id anywhere in a
10-entry batch stalled delivery to every other, unrelated user in that
same batch for the whole backoff duration -- a regression introduced by
this session's own earlier ack-after-delivery fix, which made `process_one`
await `done` in the first place (previously it acked immediately after
enqueueing, which had its own, already-fixed data-loss problem).

**Fixed**: added `_process_batch()`, grouping a batch's entries by
`telegram_id` and running each user's group concurrently via
`asyncio.gather` (`_drain_one_user()` per group). A single user's own
notifications still process in their original stream order (each group is
still a sequential `for` loop internally) -- only cross-user ordering
becomes concurrent, so a 429 on one of a user's own messages can't let
their own later message jump ahead, but it also can't stall a different
user's message anymore. `run_forever()` now calls `_process_batch()`
instead of looping inline.

Added two tests to `tests/integration/test_notification_relay.py`:
`test_relay_does_not_head_of_line_block_across_users` and
`test_relay_preserves_per_user_order_when_processing_concurrently`. The
first needed a controllable stand-in (`_SlowThenFastNotifier`) rather than
the real `Notifier` -- an earlier attempt using a real `Notifier` with a
`TelegramRetryAfter`-raising fake Telegram session turned out to race
against `Notifier._run()`'s own internal queue-requeue scheduling (already
covered by `tests/unit/test_notifier.py`), producing a flaky, hard-to-reason
-about test that failed even against the fix on one run. The stub isolates
exactly what this fix changed -- notification_relay.py's own batch
concurrency -- by giving one chat_id a controlled 1-second delivery delay
and asserting the other chat_id's delivery timestamp lands almost
immediately rather than after it.

Verified the regression test is real without relying on `git stash`: the
fix introduced `_process_batch` as a new symbol, so reverting the file
would just make the test fail with `AttributeError` rather than actually
exercising the old buggy code path. Instead, wrote a throwaway script
(deleted after use) that ran the literal old-code pattern -- a plain
sequential `for` loop calling the real, unchanged `process_one()` -- against
the same `_SlowThenFastNotifier` stub. Confirmed it reproduces the bug
exactly: `a_delay=1.01s b_delay=1.01s` (B stalls behind A). The new fixed
code, by contrast, delivers B in well under 0.3s regardless of A's delay.

**Full clean-slate rebuild**: `mypy` clean (63 source files) → `pytest
tests/` (723 passed, up from 721) → `-m load` (5 passed) → `-m chaos_infra`
(1 passed) → `-m e2e` (7 passed). 736/736 total.

## 2026-08-25 — Fixed `SOCKET_TIMEOUT_SECONDS` colliding with the two `block=5000` stream reads

Fresh `/code-review high` pass over this session's own 22 commits, run by two
independent finder agents that separately converged on the same bug.

**The bug**: an earlier fix this session added a Redis client-side
`socket_timeout` (`packages/core/redis_conn.py`) after a prior review pass
caught that `get_redis()` had no timeout configured at all -- an unreachable
Redis would hang any caller forever instead of failing fast. That fix set
`SOCKET_TIMEOUT_SECONDS = 5.0`, reasoning it matched
`services/engine/commands.py`'s own `CommandTimeout` budget. It didn't
account for `services/payments/payout_worker.py` and
`services/bot/notification_relay.py`, both of which poll their own consumer
group with `xreadgroup(..., block=5000)` inside a bare `while True:` loop
with no surrounding `try`/`except`.

Confirmed by reading redis-py's actual installed source
(`redis.asyncio.connection.Connection.read_response()`), not assumed from
documentation: the async client applies `socket_timeout` as the raw socket
read deadline for every ordinary command and does not extend it for that
command's own `BLOCK` argument. The only code path that opts out (passing
`timeout=math.inf`) is PubSub's `listen()`/`get_message()` -- which is why
`commands.py`'s `send_command()`, built on `pubsub.listen()` with its own
`asyncio.wait_for()` timeout, was never at risk. The two Stream-based
blocking reads were exposed: a client socket timeout of exactly 5000ms
racing a `BLOCK` window of exactly 5000ms meant an ordinary idle stream --
not a Redis outage, the ubiquitous "nothing to do yet" case -- could raise
an unhandled `redis.exceptions.TimeoutError` and crash either worker.
`services/engine/round_engine.py`'s own `xread(..., block=1000)` was never
at risk at either value.

Reproduced directly against the real dev Redis before fixing anything: with
`socket_timeout=5.0`, `xreadgroup(..., block=5000)` against a genuinely
empty stream raised `TimeoutError: Timeout reading from localhost:6380`
after exactly 5.00s, on every poll of an idle stream, not as a rare edge
case.

**Fixed**: raised `SOCKET_TIMEOUT_SECONDS` from `5.0` to `10.0` -- a real 2x
margin over the largest `block=` value anywhere in this codebase (5000ms),
leaving room for `round_engine.py`'s smaller 1000ms window too. Re-ran the
same ad-hoc reproduction against the fix: the identical call now completes
in 5.02s with an empty result, no exception.

Added `tests/integration/test_redis_conn.py`, asserting both the arithmetic
margin and the empirical behavior (a real `xreadgroup(..., block=5000)`
against a genuinely empty stream, through the real `get_redis()`-configured
client, must return empty rather than raise). Verified the regression test
actually regresses: stashed just `redis_conn.py` back to `5.0`, reran --
failed with `AssertionError: SOCKET_TIMEOUT_SECONDS=5.0 leaves no real
margin...` -- then popped the stash to restore the fix.

**Full clean-slate rebuild**: `docker compose down -v` → `up -d` → Postgres
ready in 1s → `alembic upgrade head` (7 migrations, clean) → `mypy` (63
source files, no issues -- note: `mypy` with no path args, since
`[tool.mypy]` scopes `files` to `packages`/`services`/`migrations` and a
bare `mypy .` misleadingly reports 539 pre-existing untyped-test errors
that aren't part of this project's actual gate) → `pytest tests/` (721
passed) → `-m load` (5 passed, clean this run) → `-m chaos_infra` (1
passed; the printed `ConnectionError` traceback is the deliberate mid-test
Redis restart being logged, not a failure) → `-m e2e` (7 passed). 734/734
total.

## 2026-08-25 — Consolidated `dashboard_summary`'s three queries into one; reviewed and declined the rest of the efficiency tail

Twenty-second follow-up to the full-platform `/code-review` entry.

**Fixed**: `dashboard_summary()`'s `stakes_today`/`payouts_today`/
`house_revenue_today` were three separate scans over `ledger_entries`
for the same day, differing only in their `WHERE a.kind = ... AND
t.kind = ...` filter. Consolidated into one query bucketing all three
sums via `FILTER (WHERE ...)` in a single pass -- verified equivalent to
the original three WHERE clauses one for one, including that
`house_revenue_today` carries no `t.kind` restriction of its own,
matching the original exactly. Extended the existing
`test_dashboard_summary_reflects_real_state` test with a real
before/after assertion on `stakes_today` (this session's shared ledger
makes an absolute total meaningless, so a delta across a real stake is
what's actually verified) -- confirmed it passes identically against
both the old three-query code and the new one-query code, the right
check for a pure refactor rather than a "does this fail against the old
code" check that doesn't apply when nothing was actually broken.

**Reviewed and explicitly declined the remaining efficiency-tail items**,
rather than grinding through the whole catalogued list uniformly:

- The LTV formula "duplicated" across `player_ltv()`/
  `top_players_by_ltv()` (admin reporting) and `withdrawals.py`'s
  `lifetime_in`/`lifetime_out` (the real-time, transaction-locked
  auto-approval eligibility gate) turned out on inspection to answer
  genuinely different questions using a superficially similar SQL shape
  -- "this player's overall net value" versus "has this player withdrawn
  about as much as they've deposited, safe to auto-approve." Sharing one
  helper across a lock-free reporting query and a `FOR UPDATE`
  -transaction-scoped payment gate would risk changing the money-flow
  -critical one for a stylistic win with no real consistency payoff --
  these three were never at risk of drifting out of sync with each
  other, because they were never really computing the same thing.
- `commands.py`'s `send_command()` opening a fresh Redis pub/sub
  subscription per player action instead of one long-lived per-process
  demuxer is real, but fixing it properly means redesigning the
  request/reply protocol across the gateway's whole command-dispatch
  path (a shared demuxer, message routing by request id, gateway
  startup lifecycle changes) -- not a contained change, and this
  session's own load tests (1000 concurrent joins, full rush scenarios)
  already pass within budget on the current pattern. Risk/effort
  disproportionate to a currently-undemonstrated impact.
- `fanout.py` hard-coding `DROPPABLE_TYPES` (bingo-specific message
  type knowledge) into an otherwise domain-agnostic transport is a
  design purity concern with zero live consequence: there is exactly
  one consumer of this transport in the entire codebase. Abstracting it
  away would be designing for a hypothetical reuse case that doesn't
  exist, the exact premature-abstraction this project's own engineering
  discipline explicitly rules out.
- `list_rooms`'s "two near-duplicate branches" couldn't be precisely
  relocated against either `list_rooms()` implementation in the current
  codebase (gateway's or admin's) -- rather than guess at what the
  original finding meant and risk changing the wrong thing, left
  uninvestigated.

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 720 passed / 13
deselected (unchanged -- an existing test was extended, not added to),
`-m chaos_infra` 1 passed, `-m e2e` 7 passed clean. `-m load`: the same
two already-documented latency-budget tests flaked in the full batch
again (`test_gateway_fanout.py`'s stalled-reader test, `test_load_
multiroom.py`), both clean together in isolation immediately after --
the same shared-host contention pattern from the previous several
entries, on code paths this admin-only, read-only change doesn't touch.

## 2026-08-25 — Ran `build_state_sync`'s two independent queries concurrently

Twenty-first follow-up to the full-platform `/code-review` entry, from
the efficiency/reuse tail. Pure latency win, no behavior change.

**The inefficiency**: `build_state_sync()` -- the one-message reconnect
payload every join and every recovering socket waits on -- ran its room
lookup and its latest-round lookup as two sequential `await
pool.fetchrow(...)` calls, paying for two round trips end to end where
one round trip's worth (the slower of the two) would do. Neither query
depends on the other's result; both just filter on the same `room_id`.

**Fixed**: `asyncio.gather()` over both. Safe specifically because both
calls go through `pool.fetchrow(...)` (an `asyncpg.Pool`, not a single
`asyncpg.Connection`) -- each concurrent call gets its own connection
checked out from the pool, unlike sharing one bare connection across
concurrent queries, which asyncpg does not support. Confirmed both of
`build_state_sync()`'s real callers already pass `self._pool`. No new
regression test: this changes latency, not behavior, and the existing
`state_sync` content assertions (`test_gateway_gameplay.py` and others)
already cover correctness and pass unchanged.

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 720 passed / 13
deselected (unchanged), `-m chaos_infra` 1 passed. `-m load`: the same
already-documented `test_gateway_fanout.py` stalled-reader flake in the
full batch, clean alone immediately after. `-m e2e`: one transient
Playwright timeout on `test_history_tab_shows_a_completed_round` --
*this exact test already flaked once earlier in this session's history*
on a wholly unrelated commit (the balance_update push entry, several
fixes back) -- passed cleanly both in isolation and on an immediate
full-suite rerun.

## 2026-08-25 — Stopped `notifier.py`'s `_backoff_until` dict growing forever

Twentieth follow-up to the full-platform `/code-review` entry, another
item from the catalogue's own lowest-priority efficiency/reuse tail --
a real, if slow, resource leak in a long-running singleton process, not
a functional bug.

**The issue**: `_backoff_until[message.chat_id] = ...` gets set whenever
a chat triggers a `TelegramRetryAfter` (429), but nothing ever removed
the entry once its window passed. `Notifier` is a single long-running
process-lifetime object -- over weeks or months, every chat_id that ever
triggered even one 429 left a permanent entry, for as long as the
process stays up.

**Fixed**: once a message for a chat_id is dequeued and its
`backoff_until` has already passed, the entry is now deleted rather than
just silently ignored. This closes the leak for the common case (a chat
that got 429'd is, by definition, one this worker was actively sending
to, so another message for it dequeuing again soon is the expected
case) without adding a periodic full-dict sweep for the smaller residual
case of a chat that happens to never send another message again after
its one 429 -- documented honestly as "not fully unbounded anymore, but
not a hard zero either," matching how this fix was actually scoped
rather than overstating it.

**Regression test confirmed against the unfixed code before trusting
it**: reused the existing 429-then-succeeds scenario
(`test_retry_after_backs_off_then_eventually_succeeds`'s own setup) and
added an assertion that the chat_id is gone from `_backoff_until`
afterward. Failed against the unfixed code exactly as expected -- the
entry was still there.

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 720 passed / 13
deselected (up from 719), `-m chaos_infra` 1 passed, `-m e2e` 7 passed
clean. `-m load`: two already-documented latency-budget tests
(`test_gateway_fanout.py`'s stalled-reader test, `test_load_multiroom
.py`) flaked in the full batch again, both passing cleanly together in
isolation immediately after -- the same shared-host contention pattern
from the previous two entries, on code paths this notifier-only change
doesn't touch at all.

## 2026-08-25 — Two reuse/consistency fixes from the efficiency tail: consolidated `get_user_detail`'s balance lookup, closed `approve_withdrawal`'s missing reason check

Nineteenth follow-up to the full-platform `/code-review` entry, working
into the catalogue's own lowest-priority "efficiency/reuse" tail now
that every higher-severity item is closed. The second of these two
turned out to be a real correctness gap, not just duplication.

**`get_user_detail()` reimplemented `user_balance_snapshot`**: three
`get_or_create_account()` + `balance()` round trips, the exact shape
`packages/core/ledger.py`'s own `user_balance_snapshot()` already
replaced with a single query when it was consolidated out of
`services/gateway/queries.py` earlier in this arc. Replaced with a call
to the shared helper -- same three balance figures, one query instead of
up to nine round trips, no behavior change (existing tests already cover
the exact values returned and pass unchanged).

**`approve_withdrawal`'s missing `reason` check -- the real find here**:
the catalogue's own framing was right to flag this as more than style
duplication. `AdjustBalanceRequest`/`SetStatusRequest`/`VoidRoundRequest`
/`WithdrawalDecisionRequest` all declare `reason: str` and every route
using them enforced it non-blank with an identical `if not
body.reason.strip(): raise HTTPException(422, "reason is required")` --
except `approve_withdrawal`, which had the same required field on its
own request model but silently converted a blank reason to `None`
(`reason=body.reason or None`) instead of rejecting it. Every sibling
financial action (reject, void, adjust, set-status) requires an
accountable reason on the audit record (spec: "no hidden god mode");
nothing about releasing real money via approval is less consequential
than rejecting it. Extracted the four duplicated checks into
`_require_reason()` and added the missing fifth call to
`approve_withdrawal` -- both the dedup and the actual fix landed
together, since the duplication was directly what made the one missing
copy easy to miss in the first place.

**Regression test confirmed against the unfixed code before trusting
it**: `test_approve_withdrawal_admin_rejects_empty_reason_over_http`,
mirroring the existing `reject` equivalent -- a blank `"   "` reason over
real HTTP. Against the unfixed code this returned `200`, not `422`; the
withdrawal would have gone on to release funds with a `None` reason on
the audit trail.

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 719 passed / 13
deselected (up from 718), `-m load` 4/5 passed cleanly, `test_gateway_
fanout.py::test_stalled_reader_does_not_delay_other_sockets` failed in
the full batch (375ms vs. 300ms) but passed cleanly alone immediately
after -- the same shared-host contention pattern this exact test was
already extensively investigated and documented for in the previous
entry, not a new concern from a change that touches neither
`fanout.py` nor `connection.py`. `-m chaos_infra` 1 passed, `-m e2e` 7
passed clean.

## 2026-08-25 — Fixed the gateway writer loop's stale-state gap, then caught and fixed a real perf regression in the fix itself

Eighteenth follow-up to the full-platform `/code-review` entry.

**The bug**: `ConnectionHandler._writer_loop()` only ever checked
`self._cq.needs_state_sync` at the top of its own `while True:` loop,
immediately before blocking on `await self._cq.queue.get()`.
`fanout.py`'s `ConnectionQueue._handle_full()` sets that flag (without
enqueueing anything) when a connection's bounded 100-message queue is
already full and a droppable (`lobby_tick`/`call`) message arrives --
the documented, deliberate backpressure behavior for a socket that's
falling behind. If the writer loop was already parked inside that
`queue.get()` call on an empty queue at the moment the flag flipped,
nothing woke it up -- it would only notice on whatever later iteration
some unrelated message happened to arrive and unblock it naturally. Near
a quiet round boundary (calls pausing before settlement), that could
leave a recovering client's board visibly stale for a real, unbounded
stretch.

**Fixed**: added `ConnectionQueue._wake_event` (an `asyncio.Event`, set
alongside the existing flag in `_handle_full()`'s droppable branch) and
`get_or_wake()`, which races the next queued message against that event
and returns `None` instead of blocking indefinitely when woken by the
flag rather than a real item. `_writer_loop()` now calls this instead of
the bare `queue.get()`; its own top-of-loop `needs_state_sync` check
(unchanged) is still what actually acts on the flag -- `get_or_wake()`
only exists to make sure that check runs promptly instead of waiting on
whatever unrelated traffic happens to show up next.

**A real performance regression caught by this arc's own full
clean-slate rebuild, in the fix's first draft**: racing two freshly
-created tasks via `asyncio.wait()` (plus cancelling and awaiting
whichever one lost) on *every single call* -- even when the queue
already had a message sitting there ready to return immediately -- was
measurably more expensive than the plain `queue.get()` it replaced.
`test_gateway_fanout.py::test_stalled_reader_does_not_delay_other_
sockets` (a real, load-marked test measuring exactly this: how fast 49
healthy sockets receive a broadcast after one stalled socket overflows
its queue with 150 messages) went from reliably passing to reliably
failing at ~400ms against a 300ms budget. Fixed by trying
`queue.get_nowait()` first and only paying for the two-task race when
the queue is genuinely empty -- which is the one moment a wake signal
has anything to interrupt anyway, so the race was never needed in the
common case to begin with. Rechecked clean afterward: 2/2 passes running
just the two fanout tests together, 3/3 passes running them alongside
`test_load_multiroom.py`, both repeatedly.

**Verification**: `tests/unit/test_fanout.py` (new, pure asyncio, no
Redis/Postgres needed) tests `ConnectionQueue` directly -- five tests
covering ordering, both overflow branches, and `get_or_wake()`'s actual
wake mechanism (start a call on an empty queue, confirm it's still
pending, flip the flag via `_handle_full()`, confirm a prompt `None`
return within a 1s timeout rather than a hang). Confirmed against the
unfixed code first: 4 of 5 failed with `AttributeError: 'ConnectionQueue'
object has no attribute 'get_or_wake'` (the fifth tests pre-existing,
untouched overflow behavior, correctly still passing either way).

**Residual environmental flakiness, documented honestly rather than
chased further**: even after the perf fix, the *full* `-m load` batch
(all five load tests run back to back, the worst case for host
contention this project's own test-marker docs already call out) showed
inconsistent failures across three reruns -- sometimes all three
latency-budget tests over budget together (as much as 327ms, including
`test_load_multiroom.py`, which is on a code path this fix never
touches and has independently flaked on multiple unrelated commits
earlier in this exact session), sometimes only one, the specific test
varying each time. That inconsistency -- never the same failure twice,
never isolated to just the code this fix touches -- is the signature of
contention, not a deterministic bug: confirmed via `docker ps` that the
same already-documented `santim-commerce-*`/`spos-*` containers were
present throughout, and via direct, repeated isolated/small-group reruns
of exactly the fanout tests, which passed cleanly every time. Not
declared clean lightly -- this is the second real regression this
specific finding surfaced (the writer-loop bug, then the fix's own perf
cost), so it earned more scrutiny than a single-flake dismissal would
usually get.

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 718 passed / 13
deselected (up from 713 -- the five new `test_fanout.py` tests),
`-m chaos_infra` 1 passed, `-m e2e` 7 passed clean. `-m load`: clean in
every isolated/small-group rerun; inconsistent in the full five-test
batch as described above.

## 2026-08-25 — Gave the Mini App's WS client a terminal state for auth failures it can never recover from by retrying

Seventeenth follow-up to the full-platform `/code-review` entry, and the
second half of the reconnect-storm finding -- the first half (flat,
unbacked-off retries) was fixed earlier in this arc; this closes the
other half of that same finding: retries that can structurally never
succeed weren't being distinguished from ones that just need to wait out
a blip.

**The bug**: `services/gateway/connection.py`'s `_handshake()` closes
the socket with 4000 (malformed/unexpected first frame), 4001 (no auth
frame within the timeout), or 4003 (rejected `initData`, most commonly
past Telegram's own validity window) for failures retrying can never fix
-- `ws.js` retried all three identically to an ordinary transient drop.
`initData` is captured once, at `connect()` time, from whatever Telegram
handed the page on load; this module has no way to obtain a fresh one
without the page itself reloading (Telegram doesn't push an updated
`initData` into an already-open WebView). So a client whose captured
`initData` went stale retried forever (backed off, after the earlier fix
in this arc, but still forever) against a handshake that could only ever
fail again -- with nothing telling the player why the app just sat there.

**Fixed**: added `_TERMINAL_CLOSE_CODES = new Set([4000, 4001, 4003])`,
matching the gateway's own three codes exactly. The `close` handler now
checks this first: on a terminal code, it sets `connection: "auth_
failed"` and returns without scheduling a reconnect at all -- no more
doomed retry loop. `app.js`'s existing connection banner (already
handling `"reconnecting"`/`"offline"`) now shows a distinct message for
this state, a new `connection.expired` i18n key (added to both `en.json`
and `am.json`) telling the player to close and reopen the app -- the one
thing that would actually get them a fresh `initData` and fix it.

**Verification, matching this arc's established pattern for untestable
-in-Python client logic**: this repo has no JS test framework, so
`_TERMINAL_CLOSE_CODES` is exported and checked by a plain-node smoke
test (`tests/frontend/test_terminal_close_codes.mjs`, node's built-in
`assert` only) run via a pytest wrapper, confirming the set matches the
gateway's three codes exactly and that ordinary codes (1000, 1006, 1012)
are correctly excluded. Confirmed against the unfixed code first: import
failed with `SyntaxError: ... does not provide an export named
'_TERMINAL_CLOSE_CODES'`. Also ran the full real-browser Playwright E2E
suite (exercising this exact connection lifecycle end to end, including
a full gameplay round) to confirm the restructured `close` handler
didn't change happy-path behavior.

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 713 passed / 13
deselected (up from 712 -- the one new node-backed test), `-m load` 5
passed, `-m chaos_infra` 1 passed. `-m e2e`: flaked twice in a row on
`test_miniapp_full_gameplay_flow`, each time on a *different* specific
assertion (a visibility check, then a balance mismatch, then a full
25s page-load timeout) -- never the same failure twice, which a
deterministic regression from this change would produce. Passed cleanly
in isolation and on two further full-suite reruns (3 clean runs against
2 flaky ones); `docker ps` confirmed the same already-documented
shared-host contention (`santim-commerce-*`/`spos-*` containers) present
throughout. Reviewed the actual diff for a plausible mechanism by which
restructuring the `close` handler could affect a test that never
triggers a close event at all during a normal, uninterrupted round --
found none. Treated as the same environmental pattern documented
repeatedly elsewhere in this arc, not a regression, but flagged here
more explicitly than usual given two consecutive flakes rather than one.

## 2026-08-25 — Fixed the single most severe open finding: a "processing" payout was wrongly treated as settled

Sixteenth follow-up to the full-platform `/code-review` entry, and the
most severe item that catalogue ever flagged: real, silent, permanent,
unrecoverable player money loss, with no signal anywhere it had
happened. Fixed the money-safety half now; the visibility half now
exists too, but the *full* fix (learning what actually happened to a
"processing" transfer) is still blocked on the same external unknown
documented earlier in this arc.

**The bug**: `payout_worker.py`'s `process_one()` treated
`result.status in ("succeeded", "processing")` identically -- both
triggered `_settle_success()`: locked funds moved to
`provider_settlement`, the payment marked `'succeeded'`, the player told
`notify.withdrawal_succeeded`. But "processing" only means Chapa
*accepted* the transfer request, not that it actually completed. This
codebase has no payout webhook route and no status-polling fallback for
outbound transfers at all (unlike `deposits.py`'s own
`poll_pending_deposits()` for inbound ones), so a transfer Chapa later
actually rejected on their side (a bad account number, insufficient
float, anything) was never, ever reconciled -- the payment already said
`'succeeded'`, the money was already counted as paid out, and nothing in
this codebase would ever revisit it. A player could lose real money with
the platform's own books insisting they'd been paid.

**Fixed, deliberately not by guessing**: `"succeeded"` and
`"processing"` are now handled separately. `"processing"` no longer
calls `_settle_success()` at all -- the payment stays at
`status='processing'` (already set earlier in the same function, before
the provider call), locked funds stay exactly where they already were,
and neither a success nor a failure notification goes out, since both
would be a real claim about an outcome this worker does not actually
know. `provider_ref`/`raw_response` are still recorded, though -- an
admin resolving this manually needs Chapa's own reference to look the
transfer up with them at all. This converts an incorrect, invisible
"succeeded" into a correct, visible "not resolved yet."

**The other, necessary half -- visibility**: added
`services.admin.queries.list_stuck_processing_payouts(pool, *,
older_than_seconds=3600)`, a read-only query surfacing withdrawals stuck
at `status='processing'` past a threshold, mirroring
`list_pending_withdrawals()`'s existing shape. Deliberately read-only,
not a sweep like `sweep_stuck_approved_payouts()`: there is no safe
automated action to take on a "processing" transfer without knowing what
it actually resolved to, so this hands an admin the reference to go
check with Chapa directly rather than pretending an automated fix
exists.

**Why the *actual* remaining gap isn't closed, and isn't being
guessed at**: fully resolving this needs querying Chapa's real
transfer-status endpoint (`GET /v1/transfers/verify/<tx_ref>`, confirmed
to be the real endpoint earlier in this session's history) and mapping
its response to succeeded/failed/still-processing. That exact response
vocabulary could not be confirmed then (Chapa's docs render the response
examples in a JS-driven tabbed UI the fetch tooling available in this
environment couldn't extract, after four separate real attempts), and
remains unconfirmed now. Guessing a status mapping here is exactly the
kind of money-safety risk this whole engineering discipline exists to
avoid -- a wrong guess could misclassify a real failure as success just
as badly as the bug just fixed did. This entry closes the "wrongly
treated as settled" half unconditionally (it needed no external
information at all, just not conflating "accepted" with "completed");
the "learn the real outcome automatically" half stays open, now with a
real admin-visible queue instead of nothing.

**Regression tests, both confirmed against the unfixed code before
trusting them**: `test_processing_status_is_not_treated_as_settled`
(`test_payout_worker.py`) -- a real approved withdrawal, a fake provider
returning `"processing"`, confirms locked/cash/provider_settlement all
stay exactly where they were (a before/after delta on
`provider_settlement`, not an absolute total, since that account is
shared, real, and ever-growing across this whole session's tests) and
`provider_ref` is still recorded. Failed as `'succeeded' ==
'processing'` against the unfixed code -- the bug reproduced directly,
not inferred.
`test_list_stuck_processing_payouts_surfaces_an_unresolved_transfer`
(`test_admin_withdrawals.py`) -- a real withdrawal driven to a genuine
`"processing"` outcome through the real worker, confirms it's invisible
before the threshold and found (with the right amount and
`provider_ref`) after backdating `updated_at` past it. Failed the same
way against the unfixed code (the payment was already `'succeeded'`
before this query ever got a chance to matter).

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 712 passed / 13
deselected (up from 710), `-m load` 5 passed, `-m chaos_infra` 1 passed,
`-m e2e` 7 passed (one transient Playwright failure on
`test_miniapp_full_gameplay_flow` -- `#your-card-section` visibility,
the same test and assertion that already flaked once earlier in this
session -- passed cleanly on an immediate rerun; the same UI-timing flake
pattern, not a regression from a fix nowhere near the Mini App's own
code).

## 2026-08-25 — Fixed `update_room_admin`'s double-encoded `win_patterns` audit entries

Fifteenth follow-up to the full-platform `/code-review` entry. Audit
-readability only -- the actual room update itself was always correct;
only what the stored audit trail *showed* for this one field was wrong.

**The bug**: `update_room_admin()` built its audit `before`/`after`
dicts with a blanket `{k: str(row[k]) for k in changes}`. asyncpg
returns a `jsonb` column as a raw JSON string with no type codec
registered (the same reason `list_rooms()` a few lines above already
does its own `isinstance(..., str)` + `json.loads()`), so for
`win_patterns` specifically, `str(...)` was a no-op on an
already-a-string value -- leaving a JSON string sitting as a plain dict
value. `audit.record()`'s own `json.dumps(before)` then serialized the
*whole* dict, double-encoding that string into an escaped value inside
the stored `admin_audit_log` row: `"win_patterns": "[\"row\"]"` instead
of a clean nested array, unreadable without decoding twice -- unlike
every other field this same audit call records.

**Fixed**: added `_room_audit_value(row, key)`, applying the exact same
`isinstance(value, str)` + `json.loads()` normalization `list_rooms()`
already uses, only for `win_patterns`; every other field keeps the
existing `str(...)` treatment unchanged.

**Regression test confirmed against the unfixed code before trusting
it**: updated a real room's `win_patterns` through `update_room_admin()`,
read the stored `admin_audit_log` row back, and asserted a *single*
`json.loads()` of the whole row already produces a real Python list for
`win_patterns` -- matching how every other test in this file already
reads `before`/`after` back (one `json.loads()` call, not two). Against
the unfixed code this failed exactly as described:
`assert '["row"]' == ['row']`.

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 710 passed / 13
deselected (up from 709), `-m load` 4/5 passed cleanly, `test_load_
multiroom.py`'s p99 budget failed in the full batch (411ms vs. 300ms)
but passed cleanly alone -- confirmed via `docker ps` the same
already-documented shared-host contention, not a regression from a fix
that touches only one admin audit-log field, nowhere near the WS
call-broadcast path that test measures. `-m chaos_infra` 1 passed,
`-m e2e` 7 passed (no flake this run).

## 2026-08-25 — Fixed the admin dashboard's day-boundary timezone mismatch

Fourteenth follow-up to the full-platform `/code-review` entry.

**The bug**: `dashboard_summary()` computed "today" with Python's
`date.today()` -- whichever timezone the admin process's own host or
container happens to be in, unconfigured and never verified -- while the
SQL comparison (`e.created_at::date = $1`, and `daily_ggr()`'s identical
pattern plus `ended_at::date = $1`) casts a `timestamptz` to `date`
using the *Postgres session's own* ambient `timezone` setting, equally
unconfigured. Two independent, unconfigured defaults were never
guaranteed to agree with each other -- and even where they happened to
(both defaulting to UTC, the common case for an unconfigured container
and an unconfigured Postgres session alike, confirmed to be exactly
this deployment's actual behavior), neither matches the Ethiopian
calendar day these financial reports are actually meant to describe. A
transaction between 21:00-24:00 UTC (00:00-03:00 EAT) is a real,
everyday three-hour window, not a contrived edge case -- it happens
every single night this platform runs.

**Fixed**: both sides now compute the boundary the same explicit way.
Python side: `datetime.now(ETHIOPIA_TZ).date()` (`ETHIOPIA_TZ =
ZoneInfo("Africa/Addis_Ababa")`, UTC+3, no DST ever observed) instead of
bare `date.today()`. SQL side: `(e.created_at AT TIME ZONE
'Africa/Addis_Ababa')::date = $1` instead of the ambient-session
-dependent `e.created_at::date = $1`, applied to every one of
`dashboard_summary()`'s three queries and both of `daily_ggr()`'s.
Added `tzdata` as an explicit dependency (`pyproject.toml`) rather than
relying on the host's own `/usr/share/zoneinfo` being present -- this
project has no Dockerfile yet, so the eventual production base image is
unknown, and a minimal one may not ship system tzdata at all.

**Regression tests, and why the first draft's assertion was
wrong**: `daily_ggr(pool, on_date)` takes an explicit caller-supplied
date, making it directly, deterministically testable without needing to
mock "now" -- inserted a `house_revenue` ledger entry with `created_at`
set to a fixed `2026-08-25 23:30:00 UTC` (02:30 EAT on Aug 26) and
queried both candidate calendar days. First draft asserted the "wrong"
UTC-naive day (`Aug 25`) showed zero GGR -- failed immediately, not
against the fix but against this session's own shared test database:
other tests' real, accumulated house_revenue activity already lands on
both candidate days, since Aug 25 is this session's actual real-world
date. Fixed by snapshotting each day's GGR before and after the insert
and asserting on the *delta* (`+42.00` on the correct EAT day, `+0.00`
on the wrong UTC-naive day) rather than an absolute total, the same
ambient-noise discipline this session's other shared-database tests have
already settled on. Also added a static sanity check
(`ETHIOPIA_TZ.utcoffset(...) == +3:00:00`) as a guard against a future
edit picking a DST-observing zone by mistake. Both confirmed to fail
against the unfixed code first: the offset check with an `AttributeError`
(the constant didn't exist yet), the boundary test with the entry
landing on Aug 25 instead of Aug 26 -- confirming this deployment's
actual, current Postgres session timezone really is UTC-ambient, not a
hypothetical risk.

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 709 passed / 13
deselected (up from 707), `-m load` 5 passed, `-m chaos_infra` 1 passed,
`-m e2e` 7 passed (no flake this run).

## 2026-08-25 — Investigated `phone.py`'s Kenyan-number collision; no safe fix found, reverted the attempt

The catalogued finding: `normalize_ethiopian_phone()` accepts a
structurally-identical foreign number (its own example: a Kenyan
`07XX XXX XXX`) as if it were Ethiopian, since both countries' mobile
numbering plans happen to produce the exact same shape once normalized
(a leading `0` stripped, 9 digits, starting with `7` or `9`).

**First attempt, and why it was reverted**: removed the function's
fourth, undocumented fallback path (`else: national = digits`, accepting
a *bare* digit string with no country code and no national `0` prefix at
all -- a form the function's own docstring never claimed to support).
Looked like a clean, safe tightening with zero existing test coverage
exercising it. The routine full clean-slate rebuild caught the real
problem immediately: `services/admin/queries.py`'s `search_users()`
reuses this exact same function to normalize an *admin's free-typed
search query* before hashing it for an exact-match lookup --
specifically so an admin can search by typing just the bare 9 digits,
with no need to remember which prefix format the stored number used.
`test_search_users_finds_by_exact_phone_in_national_format` failed
immediately, for real: the "undocumented, unused" path the module's own
docstring describes (calls restricted to "a number that arrived via
Telegram's own contact-share mechanism... never free-typed text") turned
out to have a second, legitimate caller the docstring doesn't mention,
in a genuinely different context (admin search convenience, not
registration validation) with the opposite preference (accept the loose
form). Reverted immediately rather than reconciling the two callers'
conflicting needs under time pressure -- this is exactly the kind of
"looked isolated, wasn't" surprise the full clean-slate rebuild exists
to catch before it ships.

**Why the deeper, originally-cited case (the `0`-prefixed form) isn't
fixed either, and isn't a quick follow-up**: Ethiopian and Kenyan mobile
numbers in local dialing format are *genuinely structurally identical*
-- `0712345678` normalizes to a 9-digit string starting with `7` in both
countries' numbering plans. No digit-pattern check can distinguish them,
and switching to a real phone-number library (e.g. `phonenumbers`,
Google's libphonenumber port) wouldn't resolve it either: validating
against Ethiopia's own metadata answers "is this digit pattern plausible
for Ethiopia," not "is this number actually registered there" -- a
structurally-Ethiopian-shaped Kenyan number still passes either way.
Resolving this for real would need either requiring an explicit country
code on every input (which would reject legitimate Ethiopian users whose
Telegram client happens to hand over a bare national-format number, a
real if less common case -- trading one false-accept risk for a
false-reject risk on the platform's actual target users) or an external
signal this function doesn't have access to (a carrier lookup, an
IP-derived locale, an explicit user-declared country). Left as-is,
documented here rather than attempted again without one of those.
Consequence is bounded regardless: this function is only ever called on
a Telegram-verified contact-share number for registration (per its own
docstring) or an admin's own deliberate search input, and a wrongly
-accepted foreign number fails cleanly downstream at any telebirr-linked
payment step rather than causing a money-safety issue at registration
time.

No source change shipped from this entry -- `services/bot/phone.py` and
its tests are back to their pre-investigation state; `git status`
confirmed clean before moving on.

## 2026-08-25 — Logged Chapa webhook content rejections, and fixed a state-dependent test assertion found along the way

Thirteenth follow-up to the full-platform `/code-review` entry.

**The bug**: `chapa.py`'s `verify_webhook()` raises the exact same
`InvalidSignature` for a genuine forgery attempt (missing/wrong
signature headers) as it does for a *correctly signed* webhook rejected
for bad content (invalid JSON, a missing field, an unrecognized status,
a malformed amount). `handle_webhook()`'s only caller
(`services/payments/app.py`'s `chapa_webhook()` route) catches
`InvalidSignature` and returns a bare 401 with no logging at all,
deliberately for the forgery case (Chapa's own docs: don't leak which
part of a forgery attempt was wrong). But that same silence also
swallows the content-rejection case, where the request genuinely came
from Chapa (only Chapa holds the secret key that produced a valid
signature) and something about the payload itself is a real,
worth-investigating problem -- Chapa changing their status vocabulary,
or a payload shape this adapter doesn't yet handle. Both cases looked
identical in the payments service's own logs: nothing at all.

**Fixed**: added `structlog` logging (`chapa_webhook_content_rejected`,
with the reason and whatever of `tx_ref`/`reference`/`raw_status`/
`raw_amount` is available at that point) to the four rejection points
that only run *after* both signature checks already passed -- bad JSON,
missing fields, unrecognized status, malformed amount. The two
signature-check rejections above them are deliberately untouched: those
really are indistinguishable from routine forgery/scanning traffic, and
logging them the same way would misrepresent an actual attack attempt as
"huh, weird payload."

**Regression tests**: extended the three existing content-rejection
tests (`test_unrecognized_status_is_rejected_not_silently_accepted`,
`test_missing_required_field_is_rejected`,
`test_malformed_amount_is_rejected_not_an_unhandled_crash`) to assert
the expected log line using `structlog.testing.capture_logs()`, and
added `test_a_forged_signature_is_not_logged_as_content_rejected` to
confirm a genuine signature failure does *not* get logged as a content
rejection -- the fix would be actively misleading if it did. All three
extended assertions confirmed to fail against the unfixed code first
(empty `logs` list in every case) before restoring the fix.

**A real, pre-existing, state-dependent test bug found and fixed along
the way, unrelated to this fix**: the routine full clean-slate rebuild
turned up `test_full_round_35_players_ledger_balances` failing with
`Decimal('559.98') == Decimal('560.00')` -- a genuine, real 3-way tie
among the 35 real players (560.00 / 3 = 186.66 per share after rounding
down, 0.02 left over to the house, exactly matching
`settlement.split_derash()`'s own documented and independently-tested
behavior), which the test's own assertion (`sum(winners) ==
round_row["derash"]`) has apparently always been wrong for -- it just
never happened to draw a real tie in this specific test's own fixed
35-card set until this session's many added tests shifted the `rounds`
table's sequence-derived `round_id` (part of this round's deterministic
`client_seed`) far enough to land on a draw order that finally produced
one. Confirmed by rerunning in isolation (passes -- a different, earlier
`round_id`) versus in the full suite (fails -- reproducibly, not a
timing flake). Fixed by computing the actually-expected per-winner split
via `settlement.split_derash(derash, len(winners))` and comparing sorted
share lists, the same real math `test_two_simultaneous_claims_split_
derash_evenly` already exercises for exactly two winners, generalized to
however many winners a given draw actually produces.

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 707 passed / 13
deselected (up from 706), `-m load` 4/5 passed cleanly, `test_load_
multiroom.py`'s p99 budget failed in the full batch (412ms vs. 300ms)
but passed cleanly alone -- confirmed via `docker ps` the same already
-documented shared-host contention (unrelated `santim-commerce-*`/`spos-
*` containers still running on this 4-core host), not a regression from
a fix that touches only `chapa.py` and one test's assertion, nowhere
near the WS call-broadcast path that test measures. `-m chaos_infra` 1
passed, `-m e2e` 7 passed (no flake this run).

## 2026-08-25 — Fixed the Mini App's WS reconnect storm risk with exponential backoff + jitter

Twelfth follow-up to the full-platform `/code-review` entry. Client-side
(`web/miniapp/js/ws.js`), not backend -- an availability/resilience gap,
not a money-safety one.

**The bug**: `open()`'s `close` handler retried every dropped connection
after a flat, constant `RECONNECT_DELAY_MS = 1000`, forever, with zero
randomization (the one deliberate exception, code `1012` -- this
codebase's own graceful-restart signal -- correctly reconnects
immediately, and stayed that way). Every client that dropped its
connection at the same moment -- a gateway restart, a shared network
blip affecting a whole room -- was therefore retrying in lockstep,
hitting the gateway again in the same tight ~1-second-wide burst right
as it's most likely to still be fragile (cold caches, connection pool
still warming up), a real "thundering herd" risk for exactly the
scenario (many simultaneously-connected real-money players) this
platform is built around.

**Fixed**: added `_reconnectDelayForAttempt(attempt)` -- the standard
"full jitter" exponential backoff pattern: `random(0, min(30s, 1s *
2^attempt))`. A `reconnectAttempts` counter increments on every non-1012
close and resets to 0 on a successful `open`, so a healthy connection
that later drops still starts back at the short end, not wherever a
previous outage left off. The 1012 path is untouched -- still instant,
still doesn't touch the attempt counter -- since that's the server
telling the client it's specifically safe to reconnect right away, not
the general case this fix is about.

**Verification, and why it doesn't look like this session's usual
regression tests**: this repo has no JS test framework anywhere (the
Mini App is deliberately framework-free vanilla JS per `state.js`'s own
docstring) and no existing precedent for testing frontend logic outside
the Playwright E2E suite. Rather than either skip automated verification
or bolt on a new JS test framework for one function, exported
`_reconnectDelayForAttempt` and added a plain-node smoke test
(`tests/frontend/test_reconnect_backoff.mjs`, using only node's built-in
`assert` module) run via a thin pytest wrapper
(`tests/unit/test_miniapp_reconnect_backoff.py`) so it's part of the
normal `pytest tests/` pass. Confirmed against the unfixed code first:
importing the not-yet-exported function raised `SyntaxError: ... does
not provide an export named '_reconnectDelayForAttempt'` -- a real
failure, not a false negative -- before restoring the fix. Also reran
the full real-browser Playwright E2E suite (which loads and exercises
this exact file end to end, including a full gameplay round) to confirm
the edit didn't break anything a syntax-only smoke test wouldn't catch.

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 706 passed / 13
deselected (up from 705 -- the one new node-backed test), `-m load` 5
passed, `-m chaos_infra` 1 passed, `-m e2e` 7 passed (one transient
Playwright failure on `test_miniapp_full_gameplay_flow` in the full-suite
run -- a *different* test than the three prior sessions in this arc have
each independently flaked on, `#your-card-section` visibility this time
-- passed cleanly on an immediate rerun; the same UI-timing flake pattern
already documented multiple times, not a regression from a client-side
timing-only change with no visible UI difference).

## 2026-08-25 — Pushed live balance_update to every real money-moving action, not just deposits

Eleventh follow-up to the full-platform `/code-review` entry. The
largest of these follow-ups so far -- a genuine UX-correctness gap, not
a money-safety one (the ledger itself was never wrong; only what a
connected player's own screen showed them was).

**The bug**: `services/gateway/connection.py` subscribes every WebSocket
connection to a per-user `user:{user_id}` Redis pub/sub channel at
handshake, and the Mini App's header balance
(`web/miniapp/js/app.js`'s own `ws.on("balance_update", ...)` handler --
its comment already said "a deposit (or any other out-of-round balance
change) pushes this over the WS", stating the intent this code never
actually delivered) updates only when a `balance_update` message arrives
on it. Only `services/payments/deposits.py` ever published one. Staking
a card, dropping a card, winning or losing a round, requesting a
withdrawal, and a payout settling or reversing all move real money
through the same ledger but never told a connected player's UI anything
had changed -- the on-screen number stayed stale until the player
happened to reopen the wallet screen (which does its own fresh `/api/me`
fetch) or reconnect the socket. Not a money-safety bug -- every debit and
credit was still correct and enforced server-side regardless of what the
UI displayed -- but a real trust/confusion problem for a real-money app:
a player could stake a card and watch their balance appear unchanged, or
win a round and see nothing.

**Fixed**: relocated `user_balance_snapshot()` from `services/gateway
/queries.py` (which is explicitly documented as read-only Postgres
access with nothing Redis-related in it) to `packages/core/ledger.py`,
since it's a pure ledger read with nothing gateway-specific about it and
`packages/core` never depends on `services/*` -- the dependency can only
run this direction, and every service that moves money needed it, not
just the gateway. Added `ledger.publish_balance_update(pool, redis,
user_id)` alongside it (snapshot + `redis.publish` to the same
`user:{user_id}` channel deposits.py already used) and called it from
every other place a player's own balance actually changes:
`round_engine.py`'s `join()`, `drop_card()`, `_settle_with_winners()`,
and both refund-triggering paths (`_run_lobby`'s lobby-underfilled
branch, `_run_running`'s exhausted-no-winner branch, both captured their
entrant list before `_reset_to_idle()` clears it), `withdrawals.py`'s
`request_withdrawal()`, and `payout_worker.py`'s three outcomes
(succeeded, provider-exception-reversed, provider-rejected-reversed).
Deliberately scoped to these -- the core, high-frequency gameplay and
payment-request loop where a live update matters most -- and not to
`recovery.py`'s crash-recovery refund or the admin console's void
action: both are rare, out-of-band events where the affected player is
unlikely to even be connected at that exact moment, so the marginal UX
value doesn't currently justify threading `redis` through
`refunds.refund_round_in_transaction()`'s three call sites for it;
flagged here for a future pass if that judgment turns out wrong.

**A real performance regression caught and fixed before it shipped**:
`user_balance_snapshot()`'s original implementation (three
`get_or_create_account()` calls, each up to two round trips since the
lazy-create path is an `INSERT ... ON CONFLICT DO NOTHING` that returns
no row for an already-existing account, forcing a fallback `SELECT`,
plus three separate `balance()` calls) cost up to nine sequential
DB round trips per call. Wiring `publish_balance_update()` into
`round_engine.py`'s `join()` -- explicitly documented elsewhere in this
codebase as a hot path, called on every stake -- made
`test_full_round_35_players_ledger_balances` (35 sequential joins
against a 1-second test-only lobby window) start failing with
`not_joinable`: the added per-join latency pushed 35 sequential joins
past the lobby deadline mid-loop. Rewrote `user_balance_snapshot()` as a
single query (`accounts LEFT JOIN account_balances`, defaulting a
kind with no accounts row at all to "0.00" -- the same value the
lazy-create path would itself have produced for it, just without ever
needing to write anything for a pure read) instead of adding
lobby-timing slack to paper over it; confirmed this alone was sufficient
by rerunning the previously-failing test five times in a row.

**Regression tests**: ten new tests, all confirmed to fail against the
unfixed code before being trusted (`git stash push` on every touched
source file, rerun, confirm real failures -- seven "no balance_update
seen" timeouts, two `AttributeError: module 'packages.core.ledger' has
no attribute 'user_balance_snapshot'`, one for `publish_balance_update`
-- then `git stash pop` to restore). Added a shared
`recv_balance_update(redis, user_id, trigger)` test helper
(`tests/integration/conftest.py`) that subscribes to the per-user
channel, runs the triggering action, and returns the decoded payload --
used across `test_ledger.py`, `test_round_engine.py`,
`test_payments_withdrawals.py`, and `test_payout_worker.py`. Hit one
real redis-py gotcha while writing it: `pubsub.get_message(ignore_
subscribe_messages=True, timeout=...)` only polls *once* -- if that
single poll happens to read the still-unread subscribe-acknowledgment
message instead of the real payload, it discards it and returns `None`
for that call rather than continuing to wait out the rest of `timeout`
for a real message to show up. Confirmed this directly against a real
Redis in an isolated script before believing it, and fixed the helper to
loop against an overall deadline instead of trusting one call.

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 705 passed / 13
deselected (up from 695 -- the ten new tests), `-m load` 5 passed,
`-m chaos_infra` 1 passed, `-m e2e` 7 passed (no flake this run).

## 2026-08-25 — Fixed Chapa webhook's malformed-amount unhandled crash

Tenth follow-up to the full-platform `/code-review` entry.

**The bug**: `ChapaProvider.verify_webhook()` already checked its
required fields for *presence* (`our_ref`/`reference`/`status`/`amount`
not missing or `None`) and already guarded `status` for well-formedness
(`_map_status()` rejects anything outside the closed vocabulary,
converting a `ValueError` into `InvalidSignature`). `amount` never got
the same well-formedness treatment: a signed, structurally valid webhook
with a present-but-garbage amount (`"amount": "not-a-number"`, or any
other non-numeric string) made `Decimal(str(raw_amount))` raise
`decimal.InvalidOperation`. `handle_webhook()`'s only caller
(`services/payments/app.py`'s `chapa_webhook()` route) catches
`InvalidSignature` alone, so this specific exception type propagated
straight out as an unhandled 500 instead of the same deliberate,
discard-and-401 response every other malformed-webhook case here
already gets.

**Fixed**: wrapped the `Decimal(str(raw_amount))` conversion in a
`try/except InvalidOperation`, re-raising as `InvalidSignature` --
exactly the same conversion pattern `_map_status()`'s own
`ValueError -> InvalidSignature` handling a few lines above already
uses for the identical class of problem (present field, wrong shape).
No money-safety impact either way (nothing was ever credited off a
malformed amount -- the ledger never saw it), but this closes the gap
between "webhook is untrustworthy, we know why, discard it cleanly" and
"unhandled exception, 500, full traceback in the payments-service log
for something that isn't actually a bug in this service."

**Regression test confirmed against the unfixed code before trusting
it**: a validly-signed webhook body with `"amount": "not-a-number"`.
Against the pre-fix code this raised `decimal.InvalidOperation`
uncaught, exactly as described, not `InvalidSignature` -- confirmed
directly, then the fix restored and reconfirmed.

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 695 passed / 13
deselected (up from 694), `-m load` 5 passed, `-m chaos_infra` 1 passed,
`-m e2e` 7 passed (no flake this run).

## 2026-08-25 — Fixed `notification_relay.py`'s ack-before-delivery gap

Ninth follow-up to the full-platform `/code-review` entry.

**The bug**: `process_one()` called `await notifier.send(...)` and then
immediately acked the Redis Stream entry. But `Notifier.send()` (services
/bot/notifier.py) only ever enqueues onto its own in-memory
`asyncio.Queue` and returns -- the actual `bot.send_message()` call
happens later, asynchronously, in `Notifier`'s own background `_run()`
worker, subject to its global rate pace and per-chat 429 backoff. So the
stream entry -- the durable, redeliverable record that this notification
still needs to go out -- was marked done the instant it landed in a
plain in-memory queue with no persistence of its own. If this relay
process crashed (or the notifier's worker task died) any time between
that enqueue and the real Telegram call, the notification was lost
outright: already acked, so no redelivery on restart, and the in-memory
queue that held it is gone with the process. A user's withdrawal or
deposit could genuinely succeed with the confirmation message that was
supposed to tell them so silently vanishing.

**Fixed**: `Notifier.send()` now returns an `asyncio.Future[None]` that
`_run()` resolves once that specific message reaches a real terminal
state -- delivered, permanently dropped (blocked-by-user, or an
unretryable send error), or its retry budget exhausted (previously this
last case had no explicit handling at all; added a log line for it too,
since it's the same "when is this message actually finished" question
`_run()` already has to answer for every other exit path). The future
is *not* resolved on a requeue (an active 429 backoff, or a
`TelegramRetryAfter` with attempts still remaining) -- exactly the "not
done yet" case that must keep the stream entry unacked.
`notification_relay.py`'s `process_one()` now awaits that future before
acking. Every other caller (`services/bot/handlers.py`'s ~60 direct
command replies) just discards the returned future the same way they
already discarded the previous `None` return, so this stays fully
fire-and-forget for them -- nothing about an interactive command reply
now blocks on actual Telegram delivery or a 429 backoff sleep, only the
relay's own ack does.

**Regression test confirmed against the unfixed code before trusting
it**: a `Notifier` deliberately never `.start()`ed (nothing ever drains
its queue, standing in for "the process died before reaching this
message"), then `process_one()` called directly against a real queued
notification, wrapped in `asyncio.wait_for(..., timeout=0.3)`. Against
the pre-fix code this returned immediately with the entry already acked
(`DID NOT RAISE TimeoutError`) -- the exact bug, reproduced directly, not
inferred. Against the fix, `process_one()` correctly never completes
within the deadline, and the stream entry is confirmed still claimable
via a fresh read of the same consumer's own pending list afterward
(the same crash-recovery path a real restarted relay process would use).

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 694 passed / 13
deselected (up from 693), `-m load` 5 passed, `-m chaos_infra` 1 passed,
`-m e2e` 7 passed (one transient Playwright `Page.wait_for_selector`
timeout on `test_verify_draw_button_shows_a_verified_seed` in the
full-suite run, passed cleanly on an immediate full-suite rerun -- same
Mini App UI-timing flake pattern already documented twice before in this
arc, on two different e2e tests now, entirely disjoint from the bot
notification code this fix touches).

## 2026-08-25 — Fixed `rate_limit.allow()` to fail closed on a Redis error

Eighth follow-up to the full-platform `/code-review` entry.

**The bug**: `allow()` had no try/except of its own around its single
`redis.eval()` call. Every caller in the codebase (`services/gateway
/connection.py`'s per-message `WS_MESSAGES` check, `take_card`/`claim`'s
per-action checks, `deposits.py`'s `DEPOSIT` cap, `admin/auth.py`'s
`ADMIN_LOGIN` throttle) calls it with no try/except of its own either,
trusting it to just return `True`/`False`. The worst-hit caller is
`connection.py`'s `_message_loop()`: an unhandled exception there isn't
caught anywhere before it reaches that method's own top-level
`while True:`, so a single transient Redis hiccup on one WS message check
-- not a real outage, just one flaky round-trip -- killed that player's
*entire* connection, not just the one action that happened to hit it.

**Fixed**: wrapped the `redis.eval()` call in `try/except Exception`,
logging via `structlog` and returning `False` (fail closed) on any error.
Every existing caller's ordinary "if not allowed: send a rate_limited
error and keep going" path already does the right thing with that,
so no caller needed to change. Considered failing open instead
(treating a Redis error as "allowed") and rejected it: this bucket set
includes `ADMIN_LOGIN`'s brute-force throttle and `DEPOSIT`'s
financial-abuse cap, both real security controls, and this whole
platform already has no path to function at all without Redis (room
locking, session/command dispatch), so failing closed here doesn't
meaningfully worsen a genuine outage -- it only changes the outcome for
the transient-blip case this fix actually targets.

**Regression test confirmed against the unfixed code before trusting
it**: monkeypatched `redis.eval` to raise `ConnectionError`, matching the
exact technique the `room_lock.py` fix earlier in this arc already used
for the same class of bug. Against the pre-fix code the exception
propagated straight out of `allow()` uncaught, exactly as described,
before being fixed and reconfirmed.

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 693 passed / 13
deselected (up from 692), `-m load` 5 passed, `-m chaos_infra` 1 passed
(its own deliberately-simulated Redis-connection-reset chaos scenario
prints an expected traceback to the log, not a failure), `-m e2e` 7
passed.

## 2026-08-25 — Fixed the loss-cap TOCTOU race across concurrent joins in different rooms

Seventh follow-up to the full-platform `/code-review` entry -- the first
of these follow-ups that's a compliance/responsible-gaming control
bypass rather than a resilience or fund-safety gap; the ledger itself was
never at risk (every stake still individually debits correctly), but a
player's own declared `daily_loss_cap` -- exactly the self-protection
tool spec section 9 requires this platform to honor -- could be raced
past.

**The bug**: `RoundEngine.join()` called
`responsible_gaming.check_stake_allowed()` (which reads
`today_net_loss()`, a live aggregate over today's ledger entries) *before*
opening the transaction that actually debits the stake, with nothing
locking the gap between the two. `self._join_lock` only serializes joins
within *one* `RoundEngine` instance -- one room. It does nothing for the
same user joining two *different* rooms at the same moment, which is a
completely ordinary thing for this platform to allow (nothing stops one
player from having tabs open on two rooms at once). Two such concurrent
joins could both read `today_net_loss() == 0` before either stake
committed, and both pass a cap that either one alone would have
correctly blocked. Confirmed this is real, not theoretical, with a
regression test before writing the fix at all (see below).

**Fixed**: `join()` now takes `pg_advisory_xact_lock($1)` keyed on
`user_id`, immediately after opening the transaction and before calling
`check_stake_allowed()`. A concurrent join for the same user (any room,
any connection) now blocks on this lock until the first join's
transaction commits or rolls back, so the second one's `today_net_loss()`
read is guaranteed to see the first one's stake. Considered locking the
user's `user_cash` `account_balances` row directly instead (what
`ledger.post()` itself already does) -- rejected, because that row is
created lazily on first use and doesn't reliably exist yet for a
brand-new user, whereas an advisory lock needs no backing row. Confirmed
no other code in this codebase takes a Postgres advisory lock, so there's
no risk of the same integer key meaning two different things to two
different call sites.

**Regression test confirmed against the unfixed code before trusting
it** (same discipline as every fix in this arc): two real, real-joined
`RoundEngine` instances on two different rooms, same user, a loss cap
that one 60 ETB stake satisfies but two together violate, joined via
`asyncio.gather`. Against the pre-fix code this reliably reproduced the
bypass itself, not just a flaky race: `(JoinResult(ok=True, reason=None),
JoinResult(ok=True, reason=None))` -- both stakes succeeded, `count(True)
== 2` where the assertion requires exactly 1. Reran the fixed code five
times in a row to confirm the test isn't itself a coin-flip before
trusting a single green run, then restored the fix
(`git stash pop` after the revert-and-confirm step).

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 692 passed / 13
deselected (up from 691), `-m load` 5 passed, `-m chaos_infra` 1 passed,
`-m e2e` 7 passed.

## 2026-08-25 — Fixed `payout_worker.py`'s consumer-name-locked crash recovery via XAUTOCLAIM

Sixth follow-up to the full-platform `/code-review` entry, and the second
of two payout-pipeline gaps fixed in this pass (the other is the
`sweep_stuck_approved_payouts` entry directly below).

**The bug**: `process_next()`/`run_forever()`'s only crash-recovery step
was `xreadgroup(GROUP, consumer_name, {PAYOUT_STREAM: "0"})` -- re-reading
*this exact consumer's own* pending-entries list. That recovers a crashed
worker's in-flight job only if its replacement happens to come back up
under the identical consumer name. Real worker fleets don't guarantee
that -- a hostname- or PID-derived consumer name is the normal case, and
this codebase's own `consumer_name` parameter defaults to a literal
`"worker-1"` with no uniqueness guarantee across replicas in the first
place. Once a different consumer name picked up a job and then died
before acking, that stream entry sat in the dead consumer's PEL forever,
invisible to every other consumer's own-pending-only read, and invisible
to a fresh read too (`xreadgroup ">"` only returns entries never before
delivered to *any* consumer in the group) -- a withdrawal stuck mid-payout
indefinitely, funds already out of `user_cash`, no automatic path back.
Confirmed this behavior directly (not assumed): manually had a
`"worker-a"` consumer claim a job and never ack it, then called the
*pre-fix* `process_next(..., consumer_name="worker-b")` against the real
dev Redis -- it returned `None`, not an error, silently reporting nothing
to do.

**Fixed**: added `_claim_stale_entries()`, using `XAUTOCLAIM` to reclaim
entries idle longer than a threshold (`CLAIM_STALE_AFTER_MS = 60_000`,
matching this codebase's other "how long before we call it crashed"
thresholds) from *any* consumer in the group, not just `consumer_name`'s
own. Wired into `process_next()`/`run_forever()` as a middle step: this
consumer's own pending first, then a stale cross-consumer entry, then a
genuinely new one. Both functions gained a `claim_stale_after_ms`
parameter (default `CLAIM_STALE_AFTER_MS`) so tests can drive the claim
deterministically instead of waiting out a real 60 seconds.

**Verified XAUTOCLAIM's actual return shape against the real dev Redis**
rather than assumed from documentation: `[next_cursor, [(id, fields),
...], [deleted_ids]]` -- the middle element already matches `_flatten()`'s
existing `(msg_id, fields)` tuple shape, so no reshaping was needed in
`_claim_stale_entries()`.

**Regression test, and why it's a real one**: had a `"worker-a"` consumer
claim a job via a direct `xreadgroup(..., ">")` (simulating a crash right
after pickup, never acking), then confirmed a *different* consumer,
`"worker-b"`, via `process_next(..., claim_stale_after_ms=0)`, correctly
claims and settles it. Deliberately reverted the fix
(`git stash push -- services/payments/payout_worker.py`) and reran: the
test failed as expected -- not just a superficial failure, either, since
the reverted signature doesn't even accept `claim_stale_after_ms` at all
(`TypeError: process_next() got an unexpected keyword argument`). To make
sure that TypeError wasn't masking whether the *actual* underlying bug
would otherwise have gone undetected, re-ran the same crash scenario
manually against the real dev Redis using only the pre-fix function
signature (no `claim_stale_after_ms` argument at all): `worker-b`'s
`process_next()` returned `None` -- confirming the fix addresses a real,
reproducible gap and not just a signature mismatch -- before restoring
the fix (`git stash pop`).

Full clean-slate rebuild: `docker compose down -v` / `up -d`, migrations
clean, mypy clean across 63 source files, `pytest tests/` 691 passed / 13
deselected (up from 689 -- this fix's test plus the sweep test below),
`-m load` 5 passed, `-m chaos_infra` 1 passed, `-m e2e` 7 passed (one
transient Playwright `Page.wait_for_selector` timeout on
`test_history_tab_shows_a_completed_round` in the full-suite run, passed
cleanly both alone and on a full-suite rerun -- a UI-timing flake in a
Mini App wallet test entirely disjoint from this payout-worker change,
not a regression from it).

## 2026-08-25 — Fixed `withdrawals.py`'s post-commit `enqueue_payout` gap with a sweep

Fifth follow-up to the full-platform `/code-review` entry, and the first
of two payout-pipeline gaps fixed in this pass.

**The bug**: `request_withdrawal()` commits the DB transaction that locks
funds and sets a withdrawal to `status='approved'`, then calls
`enqueue_payout()` (a Redis `XADD`) *afterward*, outside that transaction
-- Redis isn't part of it. A crash or a Redis blip in the narrow window
between the commit and the `XADD` leaves a withdrawal stuck at
`'approved'` forever: funds already moved out of `user_cash` into
`user_locked`, but nothing ever queued to actually dispatch the payout,
and nothing else in the codebase swept for this.

**Fixed**: added `sweep_stuck_approved_payouts(pool, redis, *,
older_than_seconds=60)`, the same "poll as a fallback, not a replacement"
design `deposits.py`'s `poll_pending_deposits()` already uses -- queries
for `status='approved'` payments whose `updated_at` is older than the
threshold and re-enqueues them. Safe to run redundantly against a
withdrawal that *did* enqueue successfully: `payout_worker.process_one()`
already skips anything no longer in a pending status, and Chapa's own
`our_ref` idempotency covers a still-pending one being dispatched twice.
Returns the list of payment ids actually swept (not a count), matching
`recovery.py`'s `recover_orphaned_rounds()` convention -- deliberately,
so tests (and any future caller) can assert membership rather than an
exact total against this session's long-lived, ever-growing shared test
database.

**Two real testing pitfalls hit and fixed while writing the regression
test** (both worth recording since they're specific to this codebase's
shared dev database and module-level function design, not one-off
mistakes): (1) `monkeypatch.setattr(withdrawals, "enqueue_payout", noop)`
left in place for the whole test silently no-op'd the *sweep's own*
internal call to the same module-level name too, since both
`request_withdrawal()` and `sweep_stuck_approved_payouts()` resolve
`enqueue_payout` the same way at call time -- fixed by scoping the patch
with `monkeypatch.context()` around only the one call meant to simulate
the crash. (2) an exact-count assertion (`swept_too_soon == 0`) failed
against real ambient `'approved'` rows left over from hours earlier in
this same long-running session's shared database that also matched the
sweep's own filter -- fixed by switching `sweep_stuck_approved_payouts`'s
return type to `list[int]` (see above) and asserting membership
(`intent.payment_id in swept`) instead.

Full clean-slate rebuild: covered by the same rebuild run recorded in the
XAUTOCLAIM entry above -- both fixes landed in the same commit.

## 2026-08-25 — Fixed `recovery.py`'s room-vs-round orphan detection gap

Fourth follow-up to the full-platform `/code-review` entry.

**The bug**: `recover_orphaned_rounds()` treated "is the room's lock held
by *anyone*?" as proof a specific stuck round was still owned. A room
only ever runs one round at a time -- once a *newer* round exists for
the same room (a different, genuinely live engine claimed the room after
the stuck round's lock expired), that lock is legitimately held again,
just for the new round, not the old stuck one. Since this function only
runs once, at worker startup, the old round would be skipped forever:
its entrants' stakes left in `pot_escrow` with no remaining path to a
refund. A real risk specifically in the multi-worker fleet this whole
locking scheme (`room_lock.py`) exists to support -- this single-worker
dev/test environment can't naturally exercise the race, but the logic
bug itself doesn't depend on true multi-process concurrency to be wrong.

**Fixed**: a round now also counts as orphaned if it's no longer its
room's *latest* round (by `seq`), regardless of the room's current lock
state -- only when a round *is* the latest does the existing lock check
still apply.

**Caught a false-negative test before trusting it, again** (same
discipline as the auto-mark tie fix two entries below): reverted the fix
and reran the new regression test first, confirming it actually fails
against the unfixed code (`assert 48 in []`) before trusting that it
passing meant anything. The test spins up two real, real-joined
`RoundEngine` instances against the same room in sequence -- the first
crashed (lock deleted, matching the existing crash-recovery test's own
technique), the second genuinely live and running a newer round -- and
confirms the stuck round still gets refunded while the live one is left
alone.

Full clean-slate rebuild: mypy clean across 63 source files, `pytest
tests/` 689 passed / 13 deselected (up from 688), `-m chaos_infra` 1
passed, `-m e2e` 7 passed. `-m load`: 4/5 passed cleanly; `test_load_
multiroom.py`'s p99 budget failed in the full batch (passed cleanly
alone) -- the same already-documented shared-host contention this
session has confirmed multiple times before (unrelated projects'
containers still running on this same 4-core host), not a regression
from this change, which touches only `recovery.py`, entirely disjoint
from the WS call-broadcast path that test measures.

## 2026-08-25 — Fixed the most severe remaining catalogued finding: simultaneous auto-mark winners silently losing their share

Third follow-up to the full-platform `/code-review` entry. This was the
highest-severity item still open -- real money misallocation between two
players who both actually won, not a resilience/observability gap.

**The bug**: `_call_next_number()`'s auto-mark scan
(`for user_id, entry in list(self._entries.items()): ...`) `return`ed as
soon as the *first* winning entry's `claim()` call flipped
`self._status` away from `"running"`. Any *later* entry in that same
scan who also completed a winning pattern on this exact same called
number -- a genuine simultaneous auto-mark tie -- was never even offered
to `claim()`, silently losing that player's share of the derash to
whichever entry happened to come first in Python dict iteration order.
The manual-claim tie path was already correctly tested
(`test_two_simultaneous_claims_split_derash_evenly`); this auto-mark
equivalent wasn't, and the two paths don't share the bug because manual
claims never had this early-return short-circuit.

**Why the fix is safe**: `claim()` already handles this correctly on its
own -- a call while status is `"settling"` and still within
`WINNER_TIE_WINDOW_SECONDS` registers a genuine tie (the exact mechanism
the already-tested manual-claim path relies on). The scan loop didn't
need to short-circuit for that to work; it only needed to not give up
early. Fix: removed the `if self._status != "running": return`, so every
auto-mark-eligible entry still gets evaluated for this call regardless of
what an earlier entry in the same scan already triggered.

**Verifying the test itself was real work**: the natural approach (reuse
`test_two_simultaneous_claims_split_derash_evenly`'s technique -- cards 1
and 2, `wait_until` polling for both to become winning, matching the
existing manual-claim test) turned out not to exercise this specific bug:
that test polls the *cumulative* `_called` set from outside the call
loop, which can go true across two different calls a few numbers apart,
not necessarily the exact same `_call_next_number()` invocation this fix
is about. The first draft of the new test passed even against the
*unfixed* code, which would have been a false negative -- caught before
trusting it, not after. Redesigned to monkeypatch `bingo.winning_patterns`
so it deterministically reports both real, real-joined players' cards as
winning from the same call onward, while everything else (the real
`_call_next_number()`/`claim()`/settlement path, the real ledger, the
real database) stays genuine. Confirmed properly this time by reverting
the fix and rerunning: the test fails exactly as the bug describes
(1 winner taking the full derash instead of 2 splitting it), then passes
again once the fix is restored.

Full clean-slate rebuild: mypy clean across 63 source files, `pytest
tests/` 688 passed / 13 deselected (up from 687), `-m load` 5 passed,
`-m chaos_infra` 1 passed, `-m e2e` 7 passed.

## 2026-08-25 — Two more catalogued findings fixed: Redis connection timeouts, the `max_players` TOCTOU race

Second follow-up to the full-platform `/code-review` entry two below.

1. **`packages/core/redis_conn.py`'s `get_redis()` set no
   `socket_connect_timeout`/`socket_timeout` at all.** A degraded or
   unreachable Redis could hang any caller indefinitely instead of
   failing fast -- directly contradicting this module's own docstring
   ("if Redis is wiped, the platform must recover fully from Postgres"):
   a hang isn't a "loss" this client already knows how to survive, it's
   an outage this client itself would manufacture. **Fixed** with a 5s
   timeout (matching the real-time budget `services/engine/commands.py`'s
   own `CommandTimeout` already establishes elsewhere in this codebase).
   Verified empirically before trusting it, not assumed: confirmed
   directly against a real Redis connection that an idle
   `pubsub.listen()` (the exact blocking pattern `send_command()` and
   `FanoutHub` both depend on for potentially long idle stretches between
   messages) does *not* spuriously time out under this socket-level
   setting, including at the precise 5-second boundary where
   `send_command()`'s own Python-level `asyncio.wait_for(..., timeout=
   5.0)` could otherwise race it. The real chaos-Redis-restart test and
   the full load suite (heavy, sustained pub/sub and stream usage) both
   still pass clean.
2. **`round_engine.py`'s `join()` had no lock around its `max_players`
   capacity check.** `len(self._entries) >= self._room.max_players` was
   read, then `self._entries[user_id] = ...` written, several awaited DB
   round-trips later, with nothing serializing that window -- two
   different users joining with two different card numbers (so the
   `round_entries` UNIQUE constraint on card_no can't catch it) could
   both pass the capacity check before either updated the count,
   overfilling a room past its configured cap. In real production this
   is already effectively serialized (`_serve_commands()` consumes its
   room's command stream one entry at a time), but this codebase's own
   load/chaos tests -- and any future code path -- call `join()`
   concurrently the same way a parallelized command consumer someday
   might. **Fixed** with a new `self._join_lock` (alongside the existing
   `_round_start_lock`, which already establishes this exact pattern for
   the idle-to-lobby race) covering the capacity check through the
   `self._entries` update. New regression test fires 10 genuinely
   concurrent joins (`asyncio.gather`, not sequential) at a
   `max_players=3` room and confirms exactly 3 succeed, matching the
   in-memory count against the DB row.

Full clean-slate rebuild: mypy clean across 63 source files, `pytest
tests/` 687 passed / 13 deselected (up from 686), `-m load` 5 passed,
`-m chaos_infra` 1 passed, `-m e2e` 7 passed.

## 2026-08-25 — Three more of the previous entry's catalogued findings fixed: command isolation, room-lock split-brain, notifier resilience

Follow-up to the entry directly below. Picked off the next three safest-
to-fix-correctly items from that entry's "catalogued, not fixed" list --
all genuinely severe, but each a contained, mechanical fix (exception
handling / isolation) rather than new architecture, so safe to fix now
rather than deferring further.

1. **`round_engine.py`'s `_handle_command` had no exception isolation.**
   Any unexpected exception inside `join()`/`drop_card()`/`claim()`/
   `set_auto()` would propagate straight out of `_serve_commands()`'s
   loop and kill the room's single long-lived command consumer
   permanently (no restart) -- every subsequent command for that room
   would silently time out for players while the round itself kept
   running unattended. **Fixed**: the dispatch is now wrapped in a
   try/except that logs and returns an `internal_error` result for that
   one command, leaving the consumer loop alive for every other command.
   New regression test simulates a real exception via monkeypatching
   `engine.join` and confirms a *second*, real join still succeeds
   afterward -- proving the room survives, not just that one bad call
   fails cleanly.
2. **`room_lock.py`'s `_refresh_loop`/`release()` had no error handling
   around their Redis `eval()` calls** -- a real split-brain risk: an
   unhandled Redis error (a transient blip, not even a full outage)
   killed the refresh task *before* `self._held = False` ran, so
   `is_held()` reported `True` forever, even after the real Redis TTL
   key expired on schedule and a second engine legitimately acquired the
   same room. **Fixed**: both now treat any Redis error identically to
   "someone else already owns this lock" -- relinquish immediately,
   consistent with the module's own docstring already framing lock loss
   as the *safe* outcome of a refresh failure. Two new regression tests
   inject a real exception from the actual `eval()` call (not a
   hypothetical) and confirm `is_held()` goes `False` promptly in both
   the refresh-loop and release() paths.
3. **`notifier.py`'s worker loop only caught
   `TelegramRetryAfter`/`TelegramForbiddenError`.** Any other exception
   (e.g. `TelegramBadRequest` from malformed HTML in an interpolated user
   string, a network error) propagated straight out of the loop and
   killed the single global notification worker permanently -- nothing
   supervises or restarts it, so every future deposit/win/withdrawal
   notification for every user would silently stop until the whole
   process restarted. **Fixed**: a broad `except Exception` logs and
   drops that one message (not retried -- most causes here would never
   succeed no matter how many times retried) without killing the worker.
   New regression test injects a real `TelegramBadRequest` and confirms a
   second, unrelated message still gets sent afterward.

Still open from the previous entry's catalogue (payout reconciliation,
`enqueue_payout`'s post-commit gap, cross-consumer payout recovery,
auto-mark tie misallocation, `recovery.py`'s room-vs-round orphan gap,
`max_players` TOCTOU, the loss-cap TOCTOU, live balance-update pushes for
stakes/settlement, `notification_relay`'s ack-before-delivery gap, the
Mini App reconnect storm, `phone.py`'s non-Ethiopian-number gap, the
day-boundary timezone mismatch, and the efficiency/reuse list) -- these
remain genuinely deferred, not silently dropped; see the previous entry
for full detail on each.

Full clean-slate rebuild: mypy clean across 63 source files, `pytest
tests/` 686 passed / 13 deselected (up from 682), `-m load` 5 passed,
`-m chaos_infra` 1 passed, `-m e2e` 7 passed.

## 2026-08-25 — A full-platform `/code-review high` pass (Phase 3 through the pre-observability baseline): 5 fixed, ~20 more catalogued

Ran `/code-review high 812eb65..4cc23c4` -- the entire core platform
(realtime gateway, Mini App, deposits, withdrawals, admin console,
responsible gaming, load/chaos testing, notification relay), none of
which had ever had a structured review pass before today's earlier,
narrower one. Eight finder agents (four correctness scans split by
module area, a removed-behavior audit, a cross-file tracer, reuse, and
efficiency). This surfaced significantly more, and more severe, findings
than the earlier pass -- some touching the core game engine and payment
pipeline, the highest-stakes code in the system. Given the volume, the
five clearest, most severe, safest-to-fix-correctly findings were fixed
with full rigor (verified independently, fixed, tested, full clean-slate
rebuild); everything else is catalogued below in enough detail to act on
in a dedicated follow-up, rather than rushed into the same pass under
increasing time pressure against increasingly invasive changes.

### Fixed

1. **The most severe finding: self-exclusion was silently reversible by
   any ops/finance admin, not just superadmin.**
   `services/admin/app.py`'s generic `POST /users/{id}/status` accepted
   any string for `status` with zero validation, gated only by
   `users:suspend` (granted to `ops`/`finance`/`superadmin`, not just
   `superadmin`). `packages/core/responsible_gaming.py`'s own docstring
   states self-exclusion is irreversible specifically *because* "there is
   deliberately no 'lift my own self-exclusion' function anywhere in this
   codebase" -- this generic endpoint was exactly such a function in
   disguise: `{"status": "active", "reason": "x"}` against a
   self-excluded player instantly undid a legally-mandated exclusion,
   leaving only an audit-log entry (not a block) as a trace. **Fixed**:
   `set_user_status()` now rejects any target status outside
   `{active, limited, banned}` (excluding `self_excluded` -- setting it
   directly through this path would also be wrong, since it wouldn't run
   `self_exclude()`'s own bookkeeping and would produce a broken
   half-exclusion), and separately refuses to change status *at all* once
   a user's current status is `self_excluded`, in either direction. Three
   new tests confirm both guards and that the original bug's exact repro
   (an `active` status write against a real self-excluded user) is now
   blocked.
2. **`void_round_admin`'s audit log was not in the same transaction as
   the refund it recorded**, contradicting `services/admin/queries.py`'s
   own stated invariant ("every mutation writes an audit_log entry in the
   same transaction as the mutation itself -- never as an afterthought").
   It called `refund_round(pool, ...)` (its own independent, already-
   committed transaction) and then `audit.record(pool, ...)` afterward on
   a separate connection -- a crash in between left real money refunded
   with zero audit trail, unattributable to the admin who did it,
   exactly the kind of "hidden god mode" gap this file's own discipline
   exists to prevent. **Fixed**: `services/engine/refunds.py` gained
   `refund_round_in_transaction(conn, ...)`, the same logic taking an
   already-open connection instead of acquiring its own; `refund_round()`
   itself is now a thin wrapper around it. `void_round_admin` opens one
   transaction and calls both the refund and the audit write through the
   same `conn`. The three other callers (`round_engine.py`'s lobby-
   underfill and exhausted-draw paths, `recovery.py`'s crash sweep) were
   untouched and re-verified against the full round-engine/recovery/
   worker test suites.
3. **Admin login leaked which usernames exist via response timing.**
   `services/admin/auth.py`'s own docstring/`LoginFailed` promised "a
   caller can't use error text to enumerate valid usernames," but an
   unknown username returned `LoginFailed` immediately while a real one
   always paid bcrypt's ~100ms verification cost first -- exactly the
   signal error *text* alone doesn't leak but response *timing* does.
   **Fixed** with a fixed dummy bcrypt hash checked against any unknown-
   username attempt, paying the same cost either way. A timing assertion
   would be fragile on this session's own documented shared-host
   contention; the regression test instead spies on `_verify_password()`
   to confirm it's genuinely called (not skipped) for the unknown-
   username path.
4. **Admin login had no rate limiting or lockout at all.**
   `packages/core/rate_limit.py`'s `allow()` was never imported into
   `services/admin/app.py` -- TOTP raises the bar, but a leaked or weak
   admin password could be brute-forced online with no throttling,
   unlike every player-facing gateway action. **Fixed** with a new
   `ADMIN_LOGIN` bucket (5 attempts per 15 minutes per username --
   not one of spec 9.2's own numbers, an engineering judgment call
   closing a real gap; spec only specifies "IP allowlist" and "TOTP
   required" for admin login), checked first in `auth.login()` before any
   credential is examined, raising a distinct `LoginRateLimited` (429,
   not `LoginFailed`'s 401 -- it reveals nothing about username validity,
   only that this key has been tried too many times).
5. **Referral credit was silently dropped if the first registration
   attempt failed validation.** `services/bot/handlers.py`'s `on_contact`
   popped (deleted) the pending Redis referral *before* attempting
   registration; `ContactMismatch`/`InvalidPhone` are both explicitly
   designed to let the user retry, but by the time they did, the
   referral was already gone -- `referred_by` silently ended up `NULL`
   with no error surfaced to anyone. **Fixed**: `referral.py`'s
   `pop_pending_referral` split into `peek_pending_referral` (read-only)
   and `clear_pending_referral` (explicit delete, called only after
   registration actually records it in `users.referred_by`). New
   regression test drives the exact failed-then-successful sequence.

### Catalogued, not fixed -- for a dedicated follow-up, ranked by severity

**Game engine (the highest-stakes code left untouched this pass,
deliberately -- these need their own careful, focused sessions, not a
rushed change to the most heavily-tested core of the system):**
- `services/engine/room_lock.py`'s `_refresh_loop`/`release()` have no
  try/except around their Redis `eval()` calls. A single transient Redis
  error kills the refresh task *before* `self._held = False` runs, so
  `is_held()` reports `True` forever even after the real Redis TTL key
  expires and a second engine legitimately acquires the same room --
  split-brain double-processing of one round, risking a double payout.
  Directly contradicts the module's own docstring guarantee.
- `round_engine.py`'s auto-mark tie detection (`_call_next_number`)
  `return`s as soon as the *first* simultaneous auto-mark winner claims;
  any other entrant who also completes a winning pattern on that exact
  same called number is never evaluated in that call and has no other
  path to register within the 50ms tie window -- a real, silent
  misallocation of derash between two players who both actually won.
  The manual-claim tie path is tested; this auto-mark equivalent isn't.
- `recovery.py`'s orphan sweep treats "room lock held by *anyone*" as
  proof the *specific* stuck round is still owned. If a crashed round's
  lock expires and a *different* engine claims the room (starting a
  fresh round) before the sweep runs, the old stuck round's stakes sit
  in `pot_escrow` with no remaining path to refund -- a real risk in the
  multi-worker fleet `room_lock.py` is explicitly designed for.
- `round_engine.py`'s `max_players` capacity check
  (`len(self._entries) >= max_players`) is TOCTOU-racy against
  concurrent `join()` calls -- no lock covers the check-then-insert
  window, so a burst of joins can overfill a room past its configured
  max.
- `_serve_commands`'s call into `_handle_command` has no per-command
  exception isolation -- one malformed/edge-case command payload kills
  the room's single long-lived command consumer permanently (no
  restart), silently timing out every subsequent join/drop/claim for
  that room while the round itself keeps running unattended.

**Payments (real money-movement risk):**
- `payout_worker.py` treats Chapa's mere "accepted, processing" response
  as `_settle_success` immediately (locked funds moved to
  `provider_settlement`, payment marked `succeeded`) -- there is no
  payout webhook route and no polling fallback (unlike
  `deposits.poll_pending_deposits`) for outbound transfers, so a transfer
  that Chapa later actually fails (bad account number) is never
  reconciled: silent, permanent, unrecoverable player money loss with no
  signal anywhere.
- `withdrawals.py`'s `enqueue_payout()` (the Redis `XADD`) runs *after*
  the DB transaction that already locked the funds and inserted the
  `payments` row commits. A crash or Redis blip in between leaves the
  withdrawal stuck at `status='approved'` forever -- funds locked, no
  message ever queued, and nothing sweeps stale `approved` rows.
- `payout_worker.py` only recovers a crashed job via its *own* consumer
  name's pending-entries list (`XREADGROUP ... id="0"`) -- no
  `XCLAIM`/`XAUTOCLAIM` of another (dead) consumer's stale entries. The
  module's own docstring claims crash-redelivery works generally; it
  only holds if the replacement process reuses the exact same consumer
  name, which a real fleet (hostname-derived names) likely won't.
- `chapa.py`'s `verify_webhook` raises `InvalidSignature` (discarded, per
  the docstring, not retried or alerted on) for a structurally valid,
  correctly-signed webhook with an unrecognized status or a missing
  field -- indistinguishable from an actual forgery attempt, silently
  losing visibility into that payment's true state if Chapa ever adds a
  status value or omits a field on an edge-case transaction type.
- Lower severity: the deposit daily-cap query counts abandoned
  `pending`/`processing` intents forever (self-DoS on a user with several
  never-completed checkouts, not a security bug);
  `payout_worker.py`'s `_flatten()` assumes a specific `xreadgroup`
  return shape and would raise uncaught on an unexpected one, killing the
  worker with no restart logic.

**Responsible gaming / concurrency:**
- `check_stake_allowed()`'s daily-loss-cap read and the stake-posting
  transaction are two separate round trips with no lock spanning both --
  a player one stake from their configured cap, opening two rooms
  simultaneously, can get both stakes accepted before either read
  reflects the other, narrowly exceeding their own self-set limit. The
  ledger itself stays correct; the soft limit doesn't.

**Live updates / notifications:**
- Stakes, drop-card refunds, and settlement payouts in `round_engine.py`
  never publish the `user:{id}` Redis pub/sub event the Mini App's
  header/wallet balance listens for -- only deposits do
  (`_publish_balance_update`). A player who wins and immediately opens
  the wallet screen without a reload sees a stale balance until something
  else (a reconnect, a deposit) happens to trigger a refresh. Ledger data
  is never wrong; the live UI can be stale.
- `notifier.py`'s worker loop only catches `TelegramRetryAfter`/
  `TelegramForbiddenError`; any other exception (e.g. malformed HTML from
  unescaped user text interpolated into an HTML-parse-mode message) kills
  the single global notification worker task permanently, with nothing
  supervising or restarting it -- every future deposit/win/withdrawal
  notification for every user silently stops until the process restarts.
- `notification_relay.py` ACKs a stream entry right after
  `notifier.send()` returns, but `send()` only enqueues to an in-process
  queue -- an actual `send_message` hasn't happened yet. A crash between
  enqueue and real delivery loses the notification with no redelivery,
  since the stream entry is already ACKed.

**Gateway / Mini App:**
- `web/miniapp/js/ws.js`'s reconnect loop doesn't distinguish terminal
  auth failures (codes 4000/4001/4003) from transient disconnects --
  stale `initData` (e.g. past the 24h replay window) causes an infinite,
  unbacked-off, once-per-second reconnect storm against the gateway with
  no user-visible terminal state.
- `connection.py`'s `_writer_loop` only re-checks `needs_state_sync` at
  the top of its loop, immediately before blocking on the next queue
  item -- if a droppable-message overflow sets that flag but no further
  pub/sub traffic arrives for a while (e.g. calls pausing near round
  end), a recovering client's board stays stale until unrelated traffic
  happens to arrive.
- `services/bot/phone.py`'s Ethiopian-number validation only checks
  digit count and a leading `7`/`9` after stripping a `251`/`0` prefix --
  structurally-similar foreign numbers (e.g. a Kenyan `07XX XXX XXX`)
  normalize and get silently accepted as if Ethiopian.

**Admin console (lower severity / operational):**
- `dashboard_summary()`/`daily_ggr()` compute "today" with Python's
  `date.today()` (server-local time) while the SQL casts
  `created_at::date` using the Postgres session's own timezone setting --
  if these two clocks ever disagree, transactions near midnight get
  silently attributed to the wrong calendar day in financial reports.
- `packages/core/redis_conn.py`'s `get_redis()` sets no
  `socket_connect_timeout`/`socket_timeout` -- a degraded/unreachable
  Redis can hang admin API requests (and anything else using this client)
  indefinitely instead of failing fast.
- `update_room_admin`'s audit `before`/`after` values for the `win_patterns`
  jsonb field aren't normalized (`json.loads`'d) before being written to
  the audit log, unlike `list_rooms`'s own handling of the same column --
  a minor audit-readability inconsistency, not a financial bug.
- `packages/core/rate_limit.py`'s `allow()` has no try/except around its
  Redis `eval()` call, and no caller wraps it either -- fails *closed*
  (an unhandled exception kills the connection, not a rate-limit bypass),
  but a Redis blip becomes a mass WebSocket disconnect rather than a
  graceful degrade.
- `ADMIN_IP_ALLOWLIST` defaulting to empty ("unrestricted") is a known,
  already-documented dev-friendly tradeoff, not a newly-discovered gap --
  re-flagged here only because a production deployment that forgets to
  set it silently runs with no network-level restriction on the admin
  API at all, relying entirely on credential security.

**Efficiency/reuse (lowest priority, real but not correctness bugs):**
`user_balance_snapshot`-shaped logic (3 accounts x get-or-create + balance)
duplicated between `gateway/queries.py` and `admin/queries.py` instead of
shared; the LTV formula duplicated across `player_ltv`/`top_players_by_ltv`/
`withdrawals.py`'s `lifetime_in`/`lifetime_out`; `dashboard_summary`'s three
near-identical ledger queries collapsible into one `FILTER`-based query;
`list_rooms`'s two near-duplicate branches; the `"reason is required"`
check copy-pasted 4x in `admin/app.py` (and inconsistently *not* required
on `approve_withdrawal`, which reads like an accidental miss); settlement's
sequential per-winner account lookups in `round_engine.py`'s critical path
(could batch via `unnest()`); `fanout.py` hard-coding bingo-specific message
type knowledge (`DROPPABLE_TYPES`) into an otherwise domain-agnostic
transport; `notifier.py`'s `_backoff_until` dict growing unbounded for the
life of the process; `services/gateway/queries.py`'s `build_state_sync`
running two independent `fetchrow`s sequentially instead of via
`asyncio.gather`; `services/engine/commands.py`'s `send_command()` opening
a brand-new Redis pubsub subscription per single player action (join's
own hot path) instead of one long-lived per-process demuxer.

Full clean-slate rebuild after the five fixes: mypy clean across 63
source files, `pytest tests/` 682 passed / 13 deselected (up from 677),
`-m load` 5 passed, `-m chaos_infra` 1 passed, `-m e2e` 7 passed.

## 2026-08-25 — A broader `/code-review high` pass (backup/restore through today) caught six real bugs, including one crash

Ran `/code-review high 4cc23c4..HEAD` -- everything since backup/restore
tooling, none of which had a structured review pass yet (observability,
deposit rate limiting, phone encryption, admin reports). Eight finder
agents across three line-by-line scans, a removed-behavior audit, a
cross-file tracer, reuse/simplification, efficiency/altitude, and
conventions/test-coverage. Every genuine correctness finding was verified
independently before fixing (reproducing the exact crash, reading the
exact code paths) rather than trusted at face value. Six real bugs fixed:

1. **`services/bot/registration.py` -- a real, reachable crash in the
   registration flow, the single most significant finding.** Three call
   sites (`register_from_contact()`'s existing-user branch, its
   telegram_id-race recovery branch, and `get_registered_user()`) called
   `decrypt_phone(bytes(row["phone_e164_encrypted"]))` with no `None`
   guard, unlike the two sibling read sites
   (`services/gateway/queries.py`, `services/admin/queries.py`) that
   correctly have one. `services/gateway/queries.py`'s own
   `get_or_create_user_by_telegram_id()` lazily creates a `users` row
   with no phone at all for anyone who opens the Mini App before ever
   messaging the bot -- confirmed directly: `bytes(None)` really does
   raise `TypeError`, and that lazy-create path really does insert with
   no `phone_e164_encrypted`. Any such user sending `/start`, `/balance`,
   or any other bot command, or trying to actually complete registration
   by sharing their contact, hit an unhandled crash instead. **Fixed
   properly, not just guarded**: `register_from_contact()` now attaches
   the just-validated contact's phone to that existing phoneless row,
   actually completing registration in place (the real product-correct
   behavior, not a defensive no-op), and `get_registered_user()` treats a
   phoneless row the same as "not registered" (spec section 7.2's
   contact-share flow was never actually completed for it). Three new
   regression tests in `test_registration.py` reproduce the exact
   scenario end to end, including that a phone already used elsewhere is
   still correctly rejected for this path too.
2. **`services/admin/app.py`'s `/metrics` endpoint had no auth or IP
   allowlist at all**, unlike every other route in the file --
   `house_revenue_total` (live revenue in ETB), `deposit_outcomes_total`,
   and `payout_queue_depth` were reachable by anyone on the network with
   no session token and no IP check, a direct violation of spec 9.2's own
   "IP allowlist" requirement for the whole admin panel. Fixed by adding
   the same `_check_ip_allowlist()` every other route goes through (a
   full session isn't required, since a Prometheus scraper can't
   practically present one) -- two new tests confirm it's reachable by
   default and blocked once an allowlist is actually set, matching the
   existing `test_ip_allowlist_blocks_disallowed_source` pattern this
   endpoint had never been covered by.
3. **`services/payments/deposits.py`: a non-terminal "pending" status was
   double-counted as a real deposit failure.** `_apply_confirmed_status`
   only skipped the payment-row UPDATE for a genuinely non-terminal
   status (correct), but incremented `deposit_outcomes_total{outcome=
   "not_succeeded"}` for *any* non-succeeded status regardless (wrong) --
   so a deposit `poll_pending_deposits()` saw as "pending" (Chapa's real
   404/still-processing case) got counted as a failure, and if it later
   actually succeeded, got counted a second time as `credited` too. One
   real deposit, two outcome labels, understating the real success rate
   the Grafana dashboard shows. Fixed by returning a genuine `"pending"`
   outcome for a non-terminal status, touching neither the payment row
   nor the metric. A new regression test drives the exact pending-then-
   succeeded sequence and asserts the metric only ever moves once.
4. **`services/admin/queries.py`'s `retention_cohorts()` made an
   in-progress week indistinguishable from real churn.** A cohort that
   signed up this week showed `active_users: 0, retention_rate: 0.0` for
   every week-offset that hasn't happened yet, in the same report row as
   fully-elapsed older cohorts a reader would reasonably compare it
   against -- looking like 100% churn when the truth is "we don't know
   yet." Fixed by adding a real `elapsed` boolean per (cohort, offset)
   pair computed in the same SQL query (`cohort_week + (offset+1) weeks
   <= now()`); `retention_rate` is `null` when not elapsed, while
   `active_users` stays a real, honest count either way (activity so far
   in an in-progress week is real information; only the *rate* implies a
   completed comparison the un-elapsed case hasn't earned yet). Both
   existing tests strengthened to assert `elapsed`/`retention_rate` in
   both directions (a genuinely in-progress week and a genuinely elapsed
   one), not just `active_users`.
5. **`services/gateway/connection.py`: `take_card` acks were recorded
   under the wrong Prometheus label, and a test locked the bug in instead
   of catching it.** `_run_action()`'s `action` parameter does double
   duty -- it's both the real command name dispatched to the engine over
   Redis (`round_engine.py`'s `_handle_command` dispatches on it
   literally; `take_card` is sent as `action="join"`, matching
   `RoundEngine.join()`'s own method name) and, since this session's
   metrics work, the Prometheus label for `gateway_command_ack_seconds`.
   Every `take_card` ack was recorded under the `"join"` label, and the
   real `"join"` WS message type (handled entirely separately by
   `_handle_join()`, which never reaches this method) recorded nothing at
   all -- `tests/integration/test_metrics.py`'s own
   `test_command_ack_histogram_records_a_real_take_card_action` checked
   the `"join"` label and passed for the wrong reason, so it would have
   started failing (not catching anything) the moment this got fixed
   naively. **Fixed correctly**: the metric now labels on `ack_name`
   (already the correct, human-readable action name for every case:
   `take_card`/`drop_card`/`set_auto`/`claim`) instead of `action`,
   leaving the actual engine-command dispatch and the rate-limit scope
   (both correctly still keyed on `action="join"`) completely untouched
   -- confirmed first, before touching anything, that renaming `action`
   itself would have silently broken real take_card functionality, not
   just a metric label.
6. **`packages/core/ledger.py`: `ledger_transactions_total` was
   incremented before the transaction actually committed.** The counter
   sat inside the still-open `async with conn.transaction():` block; if
   the commit itself failed right after that point (a dropped connection,
   a DB restart), the metric would have already counted a transaction
   that never actually persisted. Low severity (observability only, not
   money-moving), but a real inconsistency with this codebase's own
   pattern elsewhere (`services/engine/refunds.py`'s
   `engine_rounds_voided_total` correctly increments only after its own
   `async with` block closes). Fixed by moving the increment to after the
   block exits.

**Not fixed, documented instead** -- real, legitimate efficiency/reuse
findings, lower severity than the six above, deliberately deferred rather
than rushed in the same pass: `phone_crypto.py`'s `_derive_key()`
re-derives the HKDF key from scratch on every call instead of caching it
(hot path: every phone read/write, and the whole migration backfill);
`player_ltv`'s "deposited minus withdrawn" formula is now independently
maintained in three places (`player_ltv`, `top_players_by_ltv`,
`withdrawals.py`'s `lifetime_in`/`lifetime_out`); the `/metrics` FastAPI
route handler is copy-pasted verbatim across `gateway/app.py`,
`payments/app.py`, and `admin/app.py`; `payout_worker.py`'s
`_settle_success`/`_reverse` share ~20 lines of near-identical
lock-accounts/post/update-payment plumbing; the deposit rate limit
consumes a token before basic validation (amount, self-exclusion,
cool-off), a deliberate ordering choice matching
`services/gateway/connection.py`'s own established rate-limit-first
pattern, confirmed intentional rather than changed; the phone-encryption
migration backfill uses a per-row Python loop rather than a set-based
UPDATE, an operational (not correctness) concern for a users table at
real production scale, which this dev environment can't meaningfully
exercise anyway.

Full clean-slate rebuild after all six fixes: mypy clean across 63
source files, `pytest tests/` 677 passed / 13 deselected (up from 671),
`-m load` 5 passed, `-m chaos_infra` 1 passed, `-m e2e` 7 passed.

## 2026-08-25 — A real `/code-review` pass caught two genuine bugs in yesterday's new test files

Ran a structured code review (`/code-review high`) against the last two
commits (the `rate_limit.py`/`logging.py`/`keyboards.py` zero-coverage
test additions) specifically to catch anything a manual read would miss.
Both findings held up under direct verification, not taken on faith:

- **`test_logging.py` calling `configure_logging()` inside the shared
  pytest process risked permanently freezing another module's logger.**
  structlog's `cache_logger_on_first_use=True` (part of
  `configure_logging()`'s own config) freezes a logger's level filter the
  first time that specific proxy is actually used -- confirmed directly:
  called `configure_logging("INFO")`, used a logger once, then called
  `configure_logging("DEBUG")` and found the *same* logger's DEBUG output
  still filtered, and confirmed `structlog.reset_defaults()` doesn't undo
  the freeze either. Every module in this codebase creates a module-level
  logger at import time (`logger = structlog.get_logger()` in
  `services/payments/deposits.py` and elsewhere); calling
  `configure_logging()` for the first time inside the single shared
  pytest process risked permanently changing whichever of those loggers
  got used next, for the rest of the whole `pytest tests/` run, depending
  on unrelated test execution order. `reconcile_job.py` (the only other
  caller) never hit this because it only ever runs in its own subprocess.
  **Fixed** by rewriting `test_logging.py` to run `configure_logging()`
  in a real, throwaway subprocess per test -- the same isolation pattern
  already established for `reconcile_job.py`'s own CLI tests and the
  Pushgateway drill, applied here for the same underlying reason
  (global, process-wide state that only a separate process can safely
  touch).
- **`test_rate_limit.py`'s `refill_per_second=0.0001` gave Redis keys
  multi-hour TTLs.** `rate_limit.py`'s own `ttl = ceil(capacity/refill)+1`
  formula turns a near-zero refill rate into a near-infinite TTL --
  confirmed directly: `redis-cli TTL` on the keys these tests had already
  created showed values up to 98824 seconds (~27.5 hours), not the
  intended "expires almost immediately." Every `pytest tests/` run was
  leaving ~7 new stale `rl:test-*` keys on the real, shared Redis
  instance every integration test uses, none of which would expire for
  most of a day. **Fixed** by replacing the magic number with a named
  `_NEGLIGIBLE_REFILL = 0.1` constant (still negligible against any
  test's actual execution time, but caps the worst-case TTL in this file
  at ~101 seconds instead of ~27.5 hours) and manually deleting the 10
  stale keys the buggy version had already left behind.

Full clean-slate rebuild after both fixes: mypy clean across 63 source
files, `pytest tests/` 671 passed / 13 deselected (unchanged -- both
fixes corrected existing tests' behavior, not their count), `-m load` 5
passed, `-m chaos_infra` 1 passed, `-m e2e` 7 passed.

## 2026-08-24 — Two more zero-coverage modules closed: `logging.py`'s redaction, `keyboards.py`

Same method as the `rate_limit.py` entry directly below: grepped every
source file against every test file to find which modules no test
anywhere actually references. Two more turned up:

- **`packages/core/logging.py`** — the redaction processor is spec
  section 9.2's own requirement ("logs must never contain full [phone]
  numbers or `initData` strings"), and it had never once been exercised:
  `reconcile_job.py` is the only existing caller of `configure_logging()`,
  and it's only ever run as a real subprocess
  (`tests/integration/test_reconcile_job.py`), so nothing ever captured
  and inspected the actual JSON a log call produces.
  `tests/unit/test_logging.py` configures real `structlog`, captures real
  stdout, and parses the real JSON line: `phone`/`phone_e164`/`init_data`/
  `bot_token`/`webhook_secret` are all confirmed redacted (including
  case-insensitively — a caller spelling a key `Phone` or `TOKEN` must
  still trip it), unrelated fields (`user_id`, `amount`) are confirmed
  left alone, and the output is confirmed real, parseable JSON with the
  expected `event`/`level`/`timestamp` shape. `structlog.configure()`
  reconfigures global state freely on every call (unlike OpenTelemetry's
  `TracerProvider`, confirmed earlier this session to have a
  first-call-wins guard) — verified this has no such guard before relying
  on it, not assumed from the two libraries sharing a "global config"
  shape.
- **`services/bot/keyboards.py`** — pure keyboard builders, zero prior
  coverage. Two behaviors worth pinning down beyond "it doesn't crash":
  `registration_keyboard`'s share button actually has
  `request_contact=True` (the entire contact-mismatch anti-spoofing check
  in `services/bot/registration.py` depends on the contact having come
  through Telegram's own share-contact UI, not user-typed text — a
  regression here would silently defeat that check while looking
  identical in the UI), and `main_menu_keyboard`'s Play button correctly
  omits `web_app` entirely when `miniapp_url` is empty rather than
  shipping a `WebAppInfo` pointing at an empty string (which Telegram's
  own client would reject) — the exact fallback the module's own comment
  already documented but nothing had verified. Also checked both locales
  actually resolve real text, not a fallback or a raw i18n key.

Full clean-slate rebuild: mypy clean across 63 source files, `pytest
tests/` 671 passed / 13 deselected (up from 657), `-m load` 5 passed,
`-m chaos_infra` 1 passed, `-m e2e` 7 passed.

## 2026-08-24 — Closed a real test-coverage gap: `packages/core/rate_limit.py` had zero tests

Found while looking for the next genuinely buildable, non-business-parameter-blocked
gap: not a single test file anywhere in the codebase referenced `rate_limit`
at all -- confirmed by grep, not assumed. The token-bucket rate limiter is
spec section 9.2's explicit security control ("claim 5/round, take_card
10/min, deposit 5/hour, WS messages 30/s"), and no existing gateway or
deposit test happens to send enough rapid requests to hit any of these
limits, so the module's actual behavior -- including the one thing the Lua
script exists to guarantee, that concurrent requests against the same
bucket can never over-grant tokens -- had never been verified, only
exercised incidentally as a side effect of other tests never happening to
trip it.

`tests/integration/test_rate_limit.py`: capacity/rejection, time-based
refill, independent buckets per key and per scope, a cost > 1 token
request, and the real concurrency test this module is actually built
for -- 50 genuinely concurrent `asyncio.gather()`'d requests against a
capacity-10 bucket let through exactly 10, never 11+, which would only be
possible if the Lua script's read-refill-check-consume cycle weren't
truly atomic. A final regression guard asserts the exact `WS_MESSAGES`/
`TAKE_CARD`/`CLAIM`/`DEPOSIT` constant values match spec 9.2's numbers
literally, since nothing else in the codebase would catch one of these
security-relevant constants quietly drifting from a typo.

Full clean-slate rebuild: mypy clean across 63 source files, `pytest
tests/` 657 passed / 13 deselected (up from 650), `-m load` 5 passed,
`-m chaos_infra` 1 passed, `-m e2e` 7 passed.

## 2026-08-24 — Admin reports: player LTV and retention cohorts (spec section 11)

Spec section 11's Reports screen lists "Daily GGR, player LTV, retention
cohorts, tax export." Only daily GGR existed before this. Bonuses/
referral rewards (spec section 8.5, and the `bonuses`/`referrals` table
stubs in section 4.5) were deliberately **not** built in this pass, and
shouldn't be confused with this entry -- that system needs real business
parameters this session has no authority to invent (bonus amounts,
wagering-requirement multipliers, referral reward sizes), the same
reasoning `risk_flags` was already left unbuilt. LTV and retention
cohorts, by contrast, are fully computable from data this system already
has (payments, users, round_entries) with no missing business input, so
they were genuinely buildable.

- **`player_ltv()`** (folded into `get_user_detail()`'s existing output)
  and **`top_players_by_ltv()`** (a ranked leaderboard for the Reports
  screen) -- net cash a player has contributed to the platform over their
  lifetime: total succeeded deposits minus total succeeded withdrawals.
  Computed directly from `payments`, not `house_revenue` -- `house_revenue`
  is one shared account, not itemized per player, so it can't answer a
  per-player question on its own.
- **`retention_cohorts()`** -- weekly signup-cohort retention: for each
  week's new signups, what fraction played at least one round (entered a
  round that actually started) in each of the following N weeks. One
  set-based SQL query (a `generate_series` cross join so every
  cohort/week-offset pair appears even at zero, not just the non-zero
  rows), not a per-user Python loop -- this report has to scale with real
  data volume, and this session's own shared dev database (tens of
  thousands of accumulated test users by now) is a real stress case for
  exactly that, not a hypothetical one.
- **A real debugging note, `generate_series`'s reserved-word/type-inference
  trap:** the first draft of the cohort SQL used `offset` as a column
  alias (a reserved word in Postgres -- syntax error) and left `$1`'s
  type ambiguous across two different arithmetic contexts in the same
  query (`asyncpg.exceptions.UndefinedFunctionError: generate_series(integer,
  double precision)` -- Postgres inferred `$1` as `double precision` from
  one usage, breaking `generate_series`'s integer-only signature
  elsewhere in the same query). Both caught by actually running the query
  against the real database before wiring it into the codebase, not by
  reading the SQL and assuming it was right.
- **A second real debugging note, from the tests themselves:** the first
  cohort-retention test backdated a user by 15 days expecting a 2-week
  offset -- wrong, because `date_trunc('week', ...)` is Monday-anchored,
  and 15 days only reliably maps to a fixed week offset when it's a
  multiple of 7 (confirmed directly against the real database: "now"
  happened to be a Monday, so 15 days landed 3 truncated weeks back, not
  2). Fixed to exactly 14 days. The LTV ranking test had a related but
  different bug: it assumed a `limit=5` leaderboard slice would contain
  both test users, but this session's own shared, ever-growing dev
  database (real accumulated payment rows from every prior test run)
  meant it sometimes wouldn't -- fixed by using a limit far larger than
  any realistic accumulated row count, so the assertion tests real DESC
  ordering without assuming either user lands in an arbitrarily small
  top-N slice.
- **Tax export was not attempted.** Unlike LTV/retention, it needs a
  specific format the Ethiopian tax authority actually requires --
  genuinely unknown here, and guessing at a compliance-facing export
  format risks producing something worse than no export at all (silently
  wrong, not obviously missing). Same reasoning as SantimPay/ArifPay:
  refuse to guess rather than fake it.

Full clean-slate rebuild: mypy clean across 63 source files, `pytest
tests/` 650 passed / 13 deselected (up from 646), `-m load` 5 passed,
`-m chaos_infra` 1 passed, `-m e2e` 7 passed.

## 2026-08-24 — Phone numbers encrypted at rest (spec 9.2), with a real product tradeoff surfaced and confirmed

Spec section 9.2: "PII: phone numbers encrypted at rest; logs must never
contain full numbers or `initData` strings." The logging half was already
done (`packages/core/logging.py`'s `_redact`); phone numbers themselves
were plain `text` in the `users` table -- a real, unaddressed gap, found
while auditing the rest of section 9.2 for what "deposit rate limiting"
(the entry below) had left unchecked.

**A genuine product decision, not an engineering default -- asked, not
guessed:** `services/admin/queries.py`'s `search_users()` supported
substring phone search (`WHERE phone_e164 ILIKE '%...%'`). Encryption
breaks that structurally: AES-GCM's random nonce means the same phone
encrypts differently every time, so equality (let alone substring
matching) can't be checked against ciphertext in SQL. Enforcing the
UNIQUE constraint and admin search at all needs a deterministic "blind
index" (a hash of the plaintext), which only ever supports *exact* match.
Presented the real tradeoff to the user directly rather than picking one:
exact-match only, keep a plaintext last-4-digits fragment, or skip
encryption to keep substring search. **Chosen: exact-match only** (the
recommended, strongest-privacy option) -- admin phone search now requires
the complete number; name and Telegram-id search are unaffected.

**`packages/core/phone_crypto.py`** -- every phone number becomes two
derived values, never one:
- `encrypt_phone()` / `decrypt_phone()`: AES-256-GCM, random 12-byte
  nonce, for confidentiality.
- `phone_lookup_hash()`: deterministic HMAC-SHA256, the "blind index"
  that carries the UNIQUE constraint and every exact-match lookup
  (registration's duplicate-phone check, admin search) without ever
  comparing ciphertext.

Both keys are derived via HKDF from one `PHONE_ENCRYPTION_KEY` root
secret (new, required `Settings` field -- no safe empty default, unlike
`PUSHGATEWAY_URL`/`OTEL_EXPORTER_ENDPOINT`, since registration cannot
function without it in *any* environment, dev/test included, the same as
`DATABASE_URL`/`REDIS_URL`). Real domain separation, not two independent
secrets to manage: a key good enough to decrypt phone numbers must never
also double as the lookup-hash key by accident.

**Migration (`1d14ec5fac7d_phone_encryption.py`)** replaces the plaintext
`phone_e164` column with `phone_e164_encrypted bytea` +
`phone_lookup_hash text UNIQUE`, backfilling every existing row through
the app's own real `encrypt_phone()`/`phone_lookup_hash()` functions
(following this repo's own precedent -- `89519947d424_cards_pool.py`
already calls app code from inside a migration). Both `upgrade()` and
`downgrade()` were verified against real seeded data, not just "the DDL
runs": seeded plaintext rows before upgrading, confirmed the backfilled
ciphertext decrypts back to the exact original numbers and the lookup
hash matches, then downgraded and confirmed the original plaintext values
were restored exactly.

Every call site touching `phone_e164` updated: `services/bot/
registration.py` (encrypts on write, decrypts on read; the
`UniqueViolationError` constraint-name check now matches
`phone_lookup_hash`, not `phone_e164`), `services/gateway/queries.py`'s
`user_phone()`, and `services/admin/queries.py`'s `search_users()` /
`get_user_detail()` (both decrypt for display; `search_users()` only
adds the exact-match `phone_lookup_hash` clause when the query string
itself normalizes to a complete, valid E.164 number -- a genuine partial
string simply matches nothing, exactly the confirmed tradeoff). 18 test
call sites across 6 files updated to write/read the new columns.

**Verified for real:** a new `test_search_users_does_not_match_a_genuine_
partial_phone` locks in the confirmed tradeoff (a 5-digit fragment no
longer matches); the existing "search by phone fragment" test was
re-examined and found to actually test a *complete* number in a different
format (bare national digits vs full E.164), not a true substring --
renamed to reflect that, since it still correctly passes under exact-match
search once normalized.

Full clean-slate rebuild (a real schema migration this time, verified
against a truly fresh database from empty): mypy clean across 63 source
files, `pytest tests/` 646 passed / 13 deselected, `-m load` 5 passed,
`-m chaos_infra` 1 passed, `-m e2e` 7 passed.

## 2026-08-24 — Deposit rate limiting: the last unenforced limit in spec 9.2's list

Spec section 9.2's rate-limit table: "per-user token buckets -- `claim`
5/round, `take_card` 10/min, `deposit` 5/hour, WS messages 30/s." Three of
four were already enforced (`services/gateway/rate_limit.py`'s `CLAIM`/
`TAKE_CARD`/`WS_MESSAGES` buckets, applied in
`services/gateway/connection.py`); `deposit 5/hour` was not applied
anywhere -- confirmed by grepping every deposit call site
(`services/bot/handlers.py`'s `/deposit` command, `services/gateway/
app.py`'s `/api/deposit` REST route) and finding no rate-limit check on
either path. A genuine, previously-unnoticed gap, not something blocked on
credentials or a product decision.

- **`packages/core/rate_limit.py`** (moved from `services/gateway/
  rate_limit.py` -- nothing about the Lua token-bucket script or its
  bucket constants is gateway-specific, and `services/payments/deposits.py`
  now needs the same utility, so it belongs where every other
  service-spanning utility in this codebase already lives). Only one
  importer needed updating (`services/gateway/connection.py`); no direct
  test file referenced the old path.
- New `DEPOSIT = {"capacity": 5, "refill_per_second": 5.0 / 3600.0}`
  bucket constant.
- **The check lives inside `deposits.create_deposit_intent()` itself**,
  first thing in the function, rather than duplicated at both call sites
  -- a single choke point that can't be forgotten by a future caller,
  matching this codebase's established "one shared path" philosophy
  (`refunds.refund_round()`, `deposits._apply_confirmed_status()`, ...).
  This required adding `redis: Redis` to `create_deposit_intent()`'s
  signature (in the `pool, redis, provider` order already established by
  `handle_webhook()` in the same file) and threading it through both real
  call sites and every test call site -- 18 call sites total across the
  codebase, all updated and re-verified.
- New `DepositRateLimited(DepositRejected)` exception; both callers map it
  to a translated rejection (`deposit.rate_limited` in the bot's
  `en.json`/`am.json`, `wallet.error.rate_limited` in the Mini App's
  locale files -- the Mini App's error handling already dynamically
  builds `wallet.error.${detail}` from the REST API's error code, so no
  JS change was needed there, only the locale string and the
  `_DEPOSIT_ERROR_CODES` dict entry).

**A real debugging detour worth recording:** the first version of the new
bot-handler test asserted the wrong outcome because of a wrong assumption
-- that `bot_setup`'s shared test `Settings` has no Chapa credentials, so
`/deposit` would hit the early "not available" guard. It doesn't:
`tests/integration/conftest.py` sets `CHAPA_API_KEY`/`PUBLIC_BASE_URL` env
vars (for the payments-app webhook tests, which do need a provider
constructed at startup) that `Settings()` picks up process-wide, so the
guard never fires and `cmd_deposit` really does construct a real
`ChapaProvider` and attempt a genuine network call -- confirmed directly
(a standalone script hit a real `ConnectTimeout` after several seconds).
Caught by actually debugging the failing assertion (`dp["settings"]`
introspection, not just re-reading the source) rather than loosening the
assertion to match whatever came back. Fixed by monkeypatching
`services.bot.handlers.ChapaProvider` for that one test (`cmd_deposit`
constructs the provider inline rather than taking it as an injected
dependency, so this is the only way to reach the real
`create_deposit_intent()` logic without touching the network) -- which
also made the test far more useful: it now proves a real 6th rapid
`/deposit` genuinely gets rejected, not just that the handler doesn't
crash on the new `redis` parameter.

**Verified for real on both call paths**, not just at the `deposits.py`
level: `tests/integration/test_bot_handlers.py::
test_deposit_command_rate_limited_after_five_in_a_row` drives 5 successful
deposits then a rejected 6th through the actual aiogram dispatcher (real
DI resolution of the new `redis` parameter, not assumed safe by analogy to
`pool`/`notifier`/`settings`); `tests/integration/test_gateway_rest.py::
test_api_deposit_rate_limited_after_five_in_a_row` proves the same thing
through the Mini App's real HTTP `/api/deposit` route.

Full clean-slate rebuild: mypy clean across 61 source files, `pytest
tests/` 645 passed / 13 deselected (up from 643), `-m load` 5/5 passed
(one instance of the same already-documented shared-host p99 contention
on `test_load_multiroom.py`, confirmed transient by immediate isolated
retry -- see the entry two below), `-m chaos_infra` 1 passed, `-m e2e` 7
passed.

## 2026-08-24 — OpenTelemetry traces for deposit and payout paths (closes spec 10.4)

Spec section 10.4's last unaddressed item: "Traces (OpenTelemetry): deposit
and payout paths end to end." Closes the section.

- **`packages/core/tracing.py`** — `configure_tracing(service_name,
  endpoint)`, opt-in like every other integration in this codebase
  (`PUSHGATEWAY_URL`, `ADMIN_IP_ALLOWLIST`, ...): empty endpoint is a
  genuine no-op, since OpenTelemetry's own API already provides a
  zero-config default no-op tracer -- every `start_as_current_span()` call
  in `deposits.py`/`withdrawals.py`/`payout_worker.py` is always safe to
  make whether or not a real collector is configured, no "is tracing on"
  branch anywhere in business logic. New `Settings.otel_exporter_endpoint`.
- **Confirmed, not assumed:** `get_tracer()` called at *module import
  time* (before any app's `lifespan()`/`build_app()` has run
  `configure_tracing()`) still correctly picks up the real provider once
  it's configured later -- `trace.get_tracer()` returns a `ProxyTracer`
  that resolves the active provider at *span-creation* time, not at
  tracer-acquisition time. Verified directly (span created before
  `set_tracer_provider()` didn't reach a real exporter; an identical span
  created after did) rather than trusted from documentation.
- **Also confirmed directly:** `trace.set_tracer_provider()` silently
  no-ops on a second call within the same process -- tracing configuration
  is genuinely process-global, once, matching how `configure_logging()`
  and every FastAPI `lifespan()` in this codebase already treat startup
  config.
- Real spans added to the actual money-moving choke points: `deposit.
  create_intent` (with a nested `deposit.provider_checkout` child spanning
  the actual provider call), `deposit.apply_confirmed_status` (outcome as
  an attribute -- `credited`/`not_succeeded`/`amount_mismatch`/`not_found`/
  `duplicate`), `withdrawal.request` (status as an attribute), and
  `payout.dispatch` (with a nested `payout.provider_call` child). Every
  span wraps a whole existing function body rather than being bolted on
  separately, so OpenTelemetry's own automatic exception-recording (a
  raised `BelowMinimumDeposit`/`KycLevelTooLow`/etc. gets attached to the
  span before propagating) covers every rejection path, not just the
  success path.
- `configure_tracing()` wired into `services/payments/app.py`'s
  `lifespan()` (service `"payments"` -- handles the deposit webhook) and
  `services/bot/app.py`'s `build_app()` (service `"bot"` -- handles
  `/deposit` and `/withdraw` command creation, per this codebase's
  existing architecture where deposit/withdrawal creation is a plain
  Python call from the bot). `payout_worker.py` gained the span *calls*
  but no configuration call of its own -- consistent with this session's
  standing scope boundary that process orchestration for background
  workers (no real CLI entrypoint exists for it) is deployment-time, not
  built here; a real deployment configures tracing before constructing a
  `PayoutWorker` the same way it would wire up its process supervisor.
- A `jaeger` service in `deploy/docker-compose.yml`
  (`jaegertracing/all-in-one:1.62.0`, confirmed to exist via a real
  `docker pull` before pinning), profile-gated the same way as the other
  observability containers. All-in-one image: OTLP/HTTP receiver, storage,
  and query UI in one container.

**Verified two ways:**
- `tests/integration/test_tracing.py` configures a real SDK
  `TracerProvider` with an `InMemorySpanExporter` once (process-global, so
  done via module-level setup with a `_clear_spans` autouse fixture
  between tests) and asserts real exported spans -- including a genuine
  parent/child relationship check (`inner.parent.span_id ==
  outer.context.span_id`) across an `await` inside the child, not just
  that two unrelated spans happen to exist -- for all four instrumented
  functions.
- **Manually, against the actual Jaeger binary:** ran a real deposit
  (`create_deposit_intent` -> `_apply_confirmed_status`), a real
  withdrawal (`request_withdrawal`), and a real payout dispatch
  (`payout_worker.process_next`) with tracing configured against a real
  running Jaeger container, then queried Jaeger's own `/api/traces` API
  and confirmed every span landed with the correct name, the correct
  nested parent/child structure, and the correct real attributes
  (`user_id`, `amount`, `our_ref`, `deposit.outcome`, `withdrawal.status`,
  `payout.outcome`). A `RecentReversibleDeposit` rejection hit during the
  drill also showed up as a real recorded exception on its span --
  confirming OpenTelemetry's automatic exception capture works here for
  real, not just as a documented feature.

**A genuine, external, non-regression finding surfaced by this pass's own
clean-slate rebuild, worth recording accurately:** the `-m load` batch's
`test_load_multiroom.py` (p99 call-to-render budget: 300ms) failed 3 times
in a row inside the full batch (430-498ms) but passed cleanly every time
run alone (see the existing note on this test's sensitivity to shared-host
contention). `ps`/`docker ps` during the failing runs showed the actual
cause: unrelated projects' containers (`santim-commerce-postgres`,
`santim-commerce-redis`, `spos-frontend`, `spos-backend`) actively running
on this same 4-core host at the time, not anything this session's changes
touched -- today's tracing work only touches
`deposits.py`/`withdrawals.py`/`payout_worker.py`, entirely disjoint from
the gateway/round-engine code path this load test exercises, and this
exact test already carried a documented "sensitive to whatever else is
sharing this process/CPU" caveat before today. Recorded as confirmation of
the existing caveat with a concrete identified cause this time, not a new
problem.

Full clean-slate rebuild: mypy clean across 61 source files, `pytest
tests/` 643 passed / 13 deselected (up from 639), `-m load` 5/5 passed in
isolation (1 failed only inside the contended batch run, per above),
`-m chaos_infra` 1 passed, `-m e2e` 7 passed.

## 2026-08-24 — Grafana dashboards, provisioned as code, verified with a real query drill (closes spec 10.4's "+ Grafana")

Spec section 10.4 pairs "Prometheus + Grafana" as one bullet; the
Prometheus half (metrics, alerts, scrape config) was built and verified
two entries below. This closes the Grafana half.

- **`deploy/grafana/provisioning/datasources/prometheus.yml`** — the
  Prometheus datasource, provisioned on container startup rather than
  clicked through by hand in the UI (and forgotten). Given a fixed
  `uid: prometheus` deliberately, not left to Grafana's auto-generated
  one -- a provisioned *dashboard* JSON has to reference its datasource by
  a stable id, and the standard `${DS_PROMETHEUS}` template-variable
  pattern only resolves on dashboard *import* through the UI, not on
  file-based provisioning (found by actually provisioning a first draft
  and getting "no data" on every panel, not by reading Grafana's docs
  fully upfront).
- **`deploy/grafana/dashboards/jo-bingo.json`** — one real dashboard, ten
  panels, one per metric `packages/core/metrics.py` defines: concurrent
  connections, rooms active, payout queue depth, live house revenue
  (stats), bingo calls/sec, ledger txn/sec by kind, command-to-ack
  latency p50/p95/p99, claim validation time p50/p95/p99, deposit success
  rate over a 15-minute window, and rounds voided per hour (time series).
  Every panel's `expr` is real PromQL against the real metric names, not
  placeholder text.
- **`deploy/grafana/provisioning/dashboards/jo-bingo.yml`** — the file
  provisioner pointing Grafana at the JSON above.
- A `grafana` service in `deploy/docker-compose.yml`
  (`grafana/grafana-oss:11.3.1` -- the exact tag confirmed to actually
  exist via a real `docker pull` before pinning it, the same discipline
  as Prometheus/Pushgateway's version pins), profile-gated the same way
  as `prometheus`/`pushgateway`, with `depends_on: [prometheus]` since an
  unprovisioned datasource would make every panel show "no data" on first
  boot. Anonymous viewer access enabled for local dev convenience (a
  throwaway `admin`/`jobingo` login also exists) -- explicitly a dev-only
  default, not a production hardening decision; a real deployment would
  set `GF_SECURITY_ADMIN_PASSWORD` from a real secret and leave anonymous
  access off.

**Verified with a real drill, matching the Prometheus entry's own
discipline:** started the real gateway app, started
prometheus+pushgateway+grafana for real, confirmed via Grafana's own
`/api/dashboards/uid/jo-bingo` that all ten panels provisioned, then
queried the *exact* panel expression the "Concurrent connections" panel
uses (`sum(gateway_connections)`) through Grafana's own datasource proxy
API (`/api/datasources/proxy/uid/prometheus/api/v1/query`) -- not a
hand-typed query against Prometheus directly, but the literal thing the
dashboard panel itself would run. Watched it go `0 -> 1 -> 0` as a real
WebSocket client connected and disconnected. This is the full chain
proven end to end: instrumentation -> `/metrics` -> Prometheus scrape ->
Grafana datasource -> dashboard panel query.

**Not built:** OpenTelemetry traces for the deposit/payout paths, the one
remaining item in spec 10.4. Real, buildable, deliberately left for a
following pass.

Full clean-slate rebuild (this pass touched only `deploy/` config, no
Python source, but re-verified anyway per this session's own discipline):
mypy clean across 60 source files, `pytest tests/` 639 passed / 13
deselected (unchanged), `-m load` 5 passed, `-m chaos_infra` 1 passed,
`-m e2e` 7 passed.

## 2026-08-24 — Closed the reconcile_job Pushgateway gap flagged in the previous entry

The observability pass just below flagged one honest gap: the
`LedgerReconciliationMismatch` alert rule referenced a
`ledger_reconciliation_mismatch_count` metric that `reconcile_job.py`
never actually pushed anywhere, since it's a one-shot CLI job with no
long-running `/metrics` endpoint of its own. Closed for real, not just
documented as future work:

- **`packages/core/metrics.py`** gained a dedicated `reconcile_registry`
  (a separate `CollectorRegistry`, not the shared default one every other
  metric in this module uses) holding just
  `ledger_reconciliation_mismatch_count` -- pushing the *shared* default
  registry from a batch job would drag along a meaningless snapshot of
  every gateway/engine/ledger/deposit counter at whatever value they
  happen to hold in that process (all zero, since reconcile_job never
  touches those code paths), which is not what a Pushgateway push is for.
- **`packages/core/reconcile_job.py`**'s `main()` now sets the gauge and,
  if `PUSHGATEWAY_URL` is configured (new `Settings.pushgateway_url`,
  default empty -- opt-in, matching every other optional integration in
  this codebase), pushes it via `prometheus_client.push_to_gateway()`
  under `job="reconcile_job"`. A push failure is caught and logged, never
  allowed to change the job's own exit code -- an observability-pipeline
  outage must never mask, or get mistaken for, a real ledger mismatch.
- **`deploy/docker-compose.yml`** gained a `pushgateway` service
  (`prom/pushgateway:v1.9.0`, also `profiles: ["observability"]`), and
  **`deploy/prometheus/prometheus.yml`** gained a scrape job for it with
  `honor_labels: true` (so a pushed job's own `job`/`instance` labels win
  over the scrape job's own -- otherwise every batch job pushed here would
  misleadingly show up labeled `job="pushgateway"`).

**Verified two ways, matching this session's usual split between a fast
automated regression test and a real manual drill against the genuine
binary:**
- `tests/integration/test_reconcile_job.py` gained two tests against a
  real (if minimal) HTTP server started in-process -- not the literal
  `prom/pushgateway` image, so the default test suite doesn't gain a new
  required docker service for one test file. One confirms a real `PUT`
  request lands with the real mismatch count in genuine Prometheus
  exposition format; one confirms no request is made at all when
  `PUSHGATEWAY_URL` is unset.
- **Manually, against the actual `prom/pushgateway` container**: ran the
  real CLI with `PUSHGATEWAY_URL` pointed at it on a healthy ledger,
  confirmed via the Pushgateway's own `/api/v1/metrics` API that
  `ledger_reconciliation_mismatch_count{job="reconcile_job"}` landed at
  `0` with `last_push_successful: true`; corrupted a real
  `account_balances` row, reran the CLI, confirmed exit code 1 and the
  Pushgateway's value updating to `1`; restored the balance and confirmed
  a clean run again. The full alert chain (job pushes -> Pushgateway holds
  -> Prometheus scrapes -> `LedgerReconciliationMismatch` rule evaluates)
  is now real and provable end to end, the one piece of spec section
  10.4's alert list that couldn't fire before this entry.

Full clean-slate rebuild: mypy clean across 60 source files, `pytest
tests/` 639 passed / 13 deselected (up from 637), `-m load` 5 passed,
`-m chaos_infra` 1 passed, `-m e2e` 7 passed.

## 2026-08-24 — Observability: real Prometheus metrics, alert rules, and a real scrape drill (spec section 10.4)

Spec section 10.4 was a genuine, unaddressed gap -- "Metrics (Prometheus +
Grafana): concurrent connections, rooms active, calls/sec, call-to-ack
p50/p95/p99, claim validation time, ledger txn/sec, deposit success rate,
payout queue depth, house revenue live... Alerts: balance reconciliation
mismatch (page immediately), payout queue depth > 50, deposit success
rate < 90%, p99 call latency > 1 s, any round voided." Unlike
SantimPay/ArifPay, nothing here needed an unreachable third-party API or a
credential this session doesn't have, so it was genuinely buildable.

**`packages/core/metrics.py`** defines every metric the spec bullet lists,
wired into the real code paths that produce each signal:
- `gateway_connections` (Gauge) -- incremented/decremented at the real
  auth-success/cleanup points in `services/gateway/connection.py`.
- `engine_rooms_active` (Gauge) -- a new `RoundEngine._set_status()`
  helper replaces every raw `self._status = ...` assignment so the gauge
  moves exactly on the idle-boundary crossing, not on all five status
  values individually.
- `engine_calls_total` (Counter), `engine_claim_validation_seconds`
  (Histogram), `engine_rounds_voided_total` (Counter) -- incremented in
  `_call_next_number()`, around `claim()`'s pattern-check block, and in
  `refunds.refund_round()` (the one shared void path every caller uses).
- `ledger_transactions_total{kind}` (Counter) -- incremented in
  `ledger.post()`, but only on a genuinely new transaction row (the
  `ON CONFLICT DO NOTHING ... RETURNING` path), never on an idempotent
  replay -- "ledger txn/sec" should mean real writes, not retries.
- `deposit_outcomes_total{outcome}` (Counter) -- incremented in
  `deposits._apply_confirmed_status()` for `credited` /
  `not_succeeded` / `amount_mismatch` only; `not_found` isn't a real
  deposit attempt on our side and `duplicate` is a replay of an outcome
  already counted once, so counting either would corrupt "deposit success
  rate."
- `payout_queue_depth` and `house_revenue_total` (Gauges) -- computed
  fresh on every `/metrics` scrape (a real `XLEN` on the payout stream, a
  real `SUM(account_balances.balance)` for `house_revenue`) rather than
  maintained incrementally, since a scrape is exactly the moment
  Prometheus wants the current value and this avoids a background polling
  loop nothing else in the process needs.

**Two interpretation calls, made explicitly rather than guessed silently:**
- **"Call-to-ack"** is read as the gateway's own command round-trip
  (join/drop_card/set_auto/claim -> the WS protocol's own `{"t": "ack",
  ...}` / `claim_result` reply), not the bingo number-call broadcast --
  the protocol already has a concrete "call ... ack" pair under that name,
  and "claim validation time" being called out as its own separate metric
  right next to it only makes sense if "call-to-ack" covers the other
  three actions too. Documented in `packages/core/metrics.py`'s own
  docstring as well.
- **"Deposit success rate"** counts only the three real terminal outcomes
  (`credited` / `not_succeeded` / `amount_mismatch`), excluding
  `not_found` and `duplicate` -- see above.

**`/metrics` endpoints** added to every service with an HTTP surface
(`services/gateway/app.py`, `services/admin/app.py`,
`services/payments/app.py` via FastAPI's `Response` +
`prometheus_client.generate_latest()`; `services/bot/app.py` via aiohttp's
`web.Response`, which -- unlike FastAPI's -- rejects a `content_type`
string with an embedded `; charset=...`, discovered by actually running
it, not by reading aiohttp's docs first). Unauthenticated, the same as the
existing `/healthz` routes -- Prometheus scraping is a network-level
concern in a real deployment, not an application-auth one, and the
admin app's optional IP allowlist (`_check_ip_allowlist`) is only wired
into `current_admin`, not a blanket middleware, so `/metrics` follows
`/healthz`'s existing precedent rather than inventing a new exposure
policy.

**`deploy/prometheus/prometheus.yml` + `alerts.yml`**, and a `prometheus`
service added to `deploy/docker-compose.yml`, gated behind a
`profiles: ["observability"]` so a plain `docker compose up -d` (used
throughout this README and every clean-slate rebuild this session runs)
doesn't start a third container nobody asked for -- confirmed for real
that explicit `docker compose up -d prometheus` still starts it despite
the profile (Compose's documented override-by-explicit-name behavior),
and that `docker compose down` needs `--profile observability` to also
tear it down, both verified by actually running each command and
inspecting `docker compose ps`, not assumed from memory. The container
reaches host-run services (this dev stack doesn't run gateway/admin/
payments/bot as long-lived docker services -- see the compose file's own
comment) via `extra_hosts: host.docker.internal:host-gateway`.

**Verified with a real drill, not just "the YAML parses":** started the
real gateway app on port 8000, started the real Prometheus container,
confirmed via Prometheus's own `/api/v1/targets` API that the gateway job
scraped successfully (admin/payments/bot correctly showed `down` -- they
weren't started for this drill). Connected a real WebSocket client and
watched `gateway_connections` go `0 -> 1 -> 0` across real scrapes via
Prometheus's own `/api/v1/query` API as the client connected and
disconnected -- proof the whole path (instrumentation -> `/metrics` ->
network reachability -> Prometheus's own scrape+storage) works end to
end, not just that each piece compiles. `/api/v1/rules` confirmed all
five alert rules loaded with `health: ok` against the real metric names.

**One honest, flagged gap, not silently skipped:** the
`LedgerReconciliationMismatch` alert rule references a
`ledger_reconciliation_mismatch_count` metric that
`packages/core/reconcile_job.py` doesn't actually push anywhere --
`reconcile_job.py` is a one-shot CLI job with no long-running `/metrics`
endpoint of its own (see the earlier reconcile_job entry below), so wiring
it up needs a real Prometheus Pushgateway (the standard pattern for batch
jobs), which isn't deployed here. The rule is written against the metric
name that integration should use, so finishing it later is "add the push
call to reconcile_job.py," not "invent the alert" -- but until that's
done, this is the one alert in the spec's own list that can never
actually fire. Not built now to avoid faking a Pushgateway integration
under time pressure; a real follow-up, not a design decision.

**Not built this pass, a reasonable next step:** Grafana dashboards
(spec pairs "Prometheus + Grafana" as one bullet) and OpenTelemetry
traces for the deposit/payout paths. Both are real, buildable, in-scope
work -- deliberately left for a following pass rather than folded into an
already-large one, the same "one well-tested concern per turn" discipline
this session has used throughout.

## 2026-08-24 — Backup/restore tooling, verified with a real drill: `deploy/backup.sh` + `deploy/restore.sh`

Spec section 14's definition of done requires: "a full restore from backup
has been performed in the last 30 days." That exact claim -- a fact about
an operating production system, observed over a real 30-day window -- isn't
something this session can manufacture: there is no production deployment,
and no 30 days have elapsed in a build session. What *is* honestly
buildable and verifiable right now is the underlying capability the claim
depends on: does a backup taken from this stack actually restore, with the
data intact, using real tooling an operator would actually run.

No backup/restore tooling existed anywhere in the repo before this --
confirmed by grepping for `pg_dump`/`pg_restore`/`backup` across the whole
tree. Built:
- **`deploy/backup.sh`** — `pg_dump -F custom` against the docker-compose
  Postgres, written to a timestamped file under `backups/` (gitignored --
  even a *test* dump can contain real-shaped user financial data and has
  no business in version control).
- **`deploy/restore.sh`** — `pg_restore` into a target database, but only
  after dropping and recreating it first rather than relying on
  `pg_restore --clean` (which only drops objects it finds a matching
  `CREATE` for in the dump, silently leaving behind anything created
  since the backup was taken -- a real restore drill has to guarantee the
  post-restore database contains *exactly* what's in the dump). Defaults
  to a separate `jobingo_restore_drill` database, never the live
  `jobingo` one, specifically so running it can never be an accidental
  overwrite of real data -- restoring over the live database requires
  naming it explicitly.

Same scope boundary as `reconcile_job.py` above: these are the actual
mechanism, invoked directly; wiring a nightly schedule around
`backup.sh` is a deployment-time decision left out of scope.

**Verified with a real drill, not just a script review:** manually, then
via `tests/integration/test_backup_restore.py` — funds a uniquely-valued
user in the real shared test database, runs `backup.sh` as a real
subprocess, restores the resulting dump into a throwaway
`jobingo_restore_drill_test` database (never touching the `jobingo`
database every other test depends on), connects to that throwaway database
directly, and asserts both the funded user's exact balance and the full
100-row cards pool survived intact. A second test confirms `restore.sh`
refuses a missing backup file outright rather than silently no-op'ing.
Both real `pg_dump`/`pg_restore` binaries (present inside the official
`postgres:15` image) are exercised for real, the same "real binary, not a
mock" discipline as every chaos test this session. Leftover dump files and
the throwaway database are always cleaned up, confirmed empty after a run.

## 2026-08-24 — Nightly ledger reconciliation job: `packages/core/reconcile_job.py`

Spec section 14's definition of done requires: "ledger sum equals balance
cache for every account, verified nightly, zero drift over 30 days."
`ledger.reconcile()` (built in Phase 0, exercised indirectly ever since by
every test that touches money) already recomputes each account's balance
from `SUM(ledger_entries.amount)` and compares it to the maintained
`account_balances` cache -- what was missing was anything that actually
runs it as a standalone job a real deployment could schedule.

This is the **first genuinely runnable CLI entrypoint in the codebase**.
Every other background process built this session -- `EngineWorker`,
`Notifier`, `payout_worker`, `notification_relay` -- exists only as a
class/function with an `async run_forever()`-shaped API; nothing wires any
of them into an actual OS process, and that's been a consistent, deliberate
scope boundary (process orchestration and deployment topology are left to
deployment time, not invented speculatively). `reconcile_job.py` is
architecturally different: it's a one-shot batch job meant to be invoked
*by* an external scheduler (cron, a systemd timer, a k8s CronJob), not a
long-running loop that needs orchestration -- so a real `if __name__ ==
"__main__":` wrapper is genuinely in scope here in a way it wasn't for the
others.

Shape: `reconcile_all(pool)` is the testable core (takes a pool directly, no
process concerns); `main()` configures structured logging, runs it, and
exits 1 with a logged `ledger_reconciliation_failed` event (including every
mismatched account id and both the cached and computed balance) on any
drift, or 0 with `ledger_reconciliation_ok` when clean. A real deployment's
scheduler should alert loudly on the non-zero exit, not retry quietly --
any drift at all means `ledger.post()`'s row-locked transactional writes
were somehow bypassed, which should never happen.

`configure_logging()` (structured JSON logging via `structlog`, built in an
earlier phase) had never actually been called anywhere in the codebase
before this -- every other module only imported `get_logger()` and relied
on whatever default logging config happened to be in effect. This job is
the first real caller of `configure_logging()`, matching how a real
process's `main()` should set up logging once, at startup.

Caught in my own test draft before ever running it: the drift-detection
tests simulate a cache/ledger mismatch by corrupting `account_balances`
directly via raw SQL (`UPDATE ... SET balance = balance + 999`), since
there's no other way to construct that state -- `ledger.post()`'s row locks
make it otherwise unreachable. That corruption runs against the same
shared, long-lived database every other test in the session also uses;
leaving it in place would have permanently failed reconciliation for every
subsequent test run until the next full `docker compose down -v`. Both
tests now wrap the corrupting `UPDATE` and the assertion in `try/finally`
so the balance is always restored.

Verified with a full clean-slate rebuild (`docker compose down -v` → `up
-d` → `alembic upgrade head` → `mypy` → full suite) specifically to make
sure this held up from a genuinely fresh database, not just the
already-warm one it was developed against: `mypy` clean across 59 source
files, `pytest tests/` 627 passed / 13 deselected, `-m load` 5 passed,
`-m chaos_infra` 1 passed, `-m e2e` 7 passed. Zero ledger drift across the
full accumulated test history of the session -- deposits, withdrawals,
stakes, payouts, refunds, and thousands of concurrent-stake contention
scenarios all reconcile cleanly.

## 2026-08-24 — SantimPay/ArifPay adapters not built: their API docs are unreachable from this environment

Attempted to build the remaining two provider adapters spec 8.1's table
calls for (SantimPay as first failover, ArifPay as second) the same way
Chapa was built in Phase 5 -- fetch the real, current API contract first,
then implement precisely against it, never against memory or a guess.
`developer.santimpay.com` failed DNS resolution; SantimPay's PyPI SDK page
and npm search both failed to load usable content; `arifpay.org/docs` 404'd
and `developer.arifpay.org` refused the connection outright. Every attempt
is a genuine, real block, not a decision to skip this.

**Deliberately not building these from an unverified guess.** This is a
real-money financial integration -- a wrong webhook signature scheme
verified against a guessed field name is a genuine forged-webhook security
hole, not a cosmetic bug, and it would look identical to a correct
implementation in tests written against my own fakes, only failing (or
worse, silently accepting a forged payload) against the real service.
`services/payments/provider.py`'s `PaymentProvider` Protocol already makes
adding either of these "a new file and nothing else" once real API access
is available -- `chapa.py` is the template to follow. Building a
`santimpay.py`/`arifpay.py` file now, unverified, would be strictly worse
than not having one: it would look done in a `git log` while carrying a
live-money risk nothing in this session could actually validate.

## 2026-08-24 — The Mini App's "Verify draw" button was dead since Phase 4 -- now real

Spec section 14's own definition of done: "a player can independently
verify any round's draw from the published seed." The button
(`#verify-draw-btn`) has existed in `index.html` since Phase 4, but
`app.js` never attached a click handler to it -- clicking it did nothing.
Exactly the "looks done, isn't" pattern the CTO instructions rule out, and
worse than no button at all since it visibly promises a capability that
silently doesn't exist. Found by checking the spec's own definition-of-done
list line by line for anything still unaddressed, not by a bug report.

Fixed for real, not by pointing at the existing admin-only fairness route:
- **New player-facing endpoint**, `GET /api/rounds/{id}/fairness` on
  `services/gateway/app.py` (`tma`-authenticated, like every other
  `/api/*` route), reusing `services/admin/queries.get_round_fairness()`
  directly rather than duplicating its logic -- there is nothing
  admin-specific in what it returns (`server_seed`, `server_seed_hash`,
  `client_seed`, `draw_order`, `verified`), since publishing exactly that
  once a round is terminal is the entire point of a commit-reveal
  provably-fair scheme. The route requires a valid session only to keep it
  off the open internet, not because any of the data is restricted to a
  particular player or the rounds they played.
- **`web/miniapp/js/state.js`'s pre-existing, always-unused `lastResult`
  field** (present since Phase 4, never once read) turned out to be built
  exactly for this: `round_end`'s handler now populates it with the full
  message (including `round_id`), and the verify button reads it back to
  know which round to ask about.
- The result screen gained a `fairness-panel` (hidden until clicked)
  showing the committed hash, the revealed seed, and a ✅/❌ verified
  indicator, plus a plain-language explainer of what "verified" actually
  means -- shown, not just asserted, per the spec's own "shown plainly"
  language reused from the reality-check requirement next to it.

**Tested with a genuine independent re-check, not just trusting the
server's own `verified` field.** `test_api_round_fairness_revealed_and_
independently_verifiable` (new, `test_gateway_rest.py`) hashes the
revealed `server_seed` itself with `hashlib.sha256` and asserts it equals
the `server_seed_hash` that was committed before the round ever ran --
the actual property "provably fair" is supposed to guarantee, checked from
outside the system under test rather than by asking the system whether it
agrees with itself. A real Chromium browser test
(`test_verify_draw_button_shows_a_verified_seed`,
`test_miniapp_e2e.py`) plays an actual round to completion, clicks the
real button, and asserts the rendered panel shows a 64-character hex seed,
a 64-character hex hash, and the ✅ indicator -- passed on the first real
run.

## 2026-08-24 — Mini App wallet: deposit/withdraw/history tabs, reality check, session reminders

Closes the last group of explicitly deferred frontend gaps: Phase 5's
deposit-amount-picker UI, and the responsible-gaming phase's reality-check
display and session-time reminders (spec section 12). Also closed two
placeholder panes that weren't explicitly promised but were sitting there
with working backends behind them (withdraw, history) -- leaving them as
"launching soon" once the real capability existed would have been exactly
the kind of stale placeholder the CTO instructions rule out.

**The Mini App had no way to create a deposit or withdrawal at all before
this pass.** The bot's `/deposit`/`/withdraw` commands call
`deposits.create_deposit_intent()`/`withdrawals.request_withdrawal()`
directly as Python (same process); the Mini App is a separate browser
context that can only reach the backend over HTTP, and no such HTTP surface
existed. Added `POST /api/deposit` and `POST /api/withdraw` to
`services/gateway/app.py` (the same `tma`-authenticated REST surface
`/api/me`/`/api/history` already established), each exception subclass
mapped to a short error code (`below_minimum`, `self_excluded`,
`insufficient_balance`, ...) the frontend looks up its own translated
message for -- the same "distinct type, not a string reason" pattern
`services/bot/handlers.py` already uses, just surfaced as JSON.

**`app.state.chapa` is now set once at gateway startup and read by both
routes, rather than each route constructing its own `ChapaProvider`
inline** (the pattern the bot's own handlers still use). This is a real,
deliberate deviation from that existing pattern, made specifically so a
test can swap in a fake provider on the running app the same way
`test_admin_app.py` already does for `app.state.ip_allowlist` -- without it,
the deposit/withdraw success path could only ever be tested at the
rejection-before-network-call layer, the same honest gap already accepted
for the bot's own `/deposit`. With it, `test_gateway_rest.py` and the new
Playwright suite both exercise the full real success path, including a
real checkout URL returned from a fake provider standing in for Chapa.

**The reality check and session-time reminders are entirely client-side,
by deliberate choice, unlike every other responsible-gaming control.**
Self-exclusion, cool-off, and the deposit/loss caps are all
server-enforced (`packages/core/responsible_gaming.py`) because they are
genuine controls a player must not be able to bypass by reloading the
page. The reality check ("net position this session") and the 60/120/180-
minute reminders are explicitly awareness nudges in spec section 12, not
enforcement -- so tracking them in `web/miniapp/js/state.js`
(`sessionStartedAt`, `sessionNetPosition`, computed entirely from
WebSocket events the client already receives) and resetting on reload is
an honest, proportionate implementation, not a corner cut. Inventing
server-side session tracking for a UX nudge would have been the actual
overreach here.

**A real, if narrow, bug fixed on the way: `showToast()`'s "is this a
translation key or an already-resolved message" heuristic was
`text.includes(".")`** -- true for every key ("error.generic") but also
true for any plain English sentence ending in a period, which the new
session-reminder toast is. Widened to also require no spaces
(`includes(".") && !includes(" ")`), which still correctly classifies
every existing call site (`msg.code` values never contain spaces; Amharic
sentences use "።" not "." and often contain spaces regardless) while
correctly leaving the reminder message alone.

**Real bugs the new E2E suite caught in itself, not in the app, all fixed
before the tests were trusted:**
- `#open-wallet-btn` only exists in the room-list header -- a test written
  to open the wallet from the *results* screen timed out because there is
  no path there in this stub (`BackButton.onClick` is a no-op stub, same
  as the rest of `prepare_page()`'s Telegram shims). Fixed the test, not
  the app: reload back to the rooms screen first, the same pattern
  `test_miniapp_e2e.py` already uses elsewhere.
- Two status-wait assertions checked `textContent.length > 0`, which the
  *intermediate* "opening…"/"submitting…" message also satisfies --
  occasionally caught mid-flight instead of the final outcome. Fixed by
  waiting for the `.error`/`.success` CSS class `setWalletStatus()` only
  ever adds once the real outcome is known.
- One assertion fuzzy-matched an expected Amharic substring ("ገምግ") that
  doesn't actually appear in the real conjugated word
  ("እየተገመገመ") -- simplified to assert on the `.success` class reaching the
  DOM (the real proof the request succeeded) rather than pattern-matching
  translated prose.

**Confirmed, not newly discovered: the pre-existing ~1-in-7-8
`test_miniapp_full_gameplay_flow` timeout (documented in Phase 4's
DECISIONS.md entry) reproduced twice more this pass** -- once immediately
after the `chaos_infra` Redis-restart test, once immediately after a full
`docker compose down -v` clean-slate rebuild, both times passing cleanly
on an immediate retry with no code change. Consistent with the existing
"shared-host contention right after something disruptive just happened"
explanation already on record; not chased further, same precedent.

## 2026-08-24 — Bot notification relay (deposit/withdrawal confirmations, deferred from Phases 5-6)

Closes the gap explicitly deferred twice already: spec 8.2 step 9 ("Bot
notification: '✅ 200 ETB deposited...'") and 8.3 step 8 ("Bot notifies the
user with the reason") were both left unbuilt because `services/payments`
and `services/admin` are separate processes from `services/bot`, and
`services/bot/handlers.py`'s own docstring makes "nothing calls
`bot.send_message` except `Notifier`" a load-bearing, tested invariant --
reaching around that from another process would have quietly broken it.
Built once for both money directions, as planned.

**`packages/core/notifications.py`** (producer) — `notify_user(conn_or_pool,
redis, *, user_id, key, **kwargs)` looks up the user's `telegram_id` and
enqueues onto a `bot_notifications` Redis Stream. Deliberately swallows
its own failures (logs, never raises): this is called *after* money has
already moved and committed, and a notification failing to enqueue must
never look like the money movement itself failed.

**`services/bot/notification_relay.py`** (consumer) — a real Redis Streams
consumer group, the same shape as `payout_worker.py`'s (the second one in
this codebase now): reads a queued notification, resolves the user's
language, and calls `Notifier.send()` -- the one and only thing in this
module allowed to do that, keeping the "nothing but `Notifier` sends"
invariant intact even for a notification that originated in a completely
different process. A Telegram private chat's id is always the user's
`telegram_id`, so no separate chat-id lookup exists.

Wired into the three places spec 8.2/8.3 actually asks for: a confirmed
deposit (`deposits.py`'s `_apply_confirmed_status`, reusing the balance
snapshot `_publish_balance_update` already computes rather than a second
query), a successful or failed payout (`payout_worker.py`'s `process_one`,
both branches), and an admin's explicit withdrawal rejection
(`admin/queries.py`'s `reject_withdrawal_admin`, the one path where a
human-authored reason is safe to show the player -- see below).

**Provider-side payout failures do not expose the raw failure reason to
the player; an admin's rejection reason does.** `notify.withdrawal_failed`
(provider errored, or reported a non-success status) is a generic message
with no `{reason}` placeholder -- the string `_reverse()` stores in
`failure_reason` is whatever the provider's exception or status string
happened to be, not something written for a player to read, and echoing
raw internal error text back to a user is its own small security/UX
smell. `notify.withdrawal_rejected` (an admin's own explicit rejection)
does include `{reason}`, because that text is deliberately authored by a
human for exactly this purpose when they reject the request.

**A third instance of the same shared-Redis-Stream test-pollution bug
already found twice this session (`payout_worker.py`'s stream in Phase 6,
now this one) -- found immediately by the relay's own tests, before
extending the fix.** The first version of `test_notification_relay.py`
had five of seven tests fail: not on any real logic, but because an
earlier test in the same file enqueued a notification without consuming
it, so a later test's `process_next()` delivered a *different* test's
stale, wrong-amount, wrong-chat-id message instead of its own. Fixed the
same way as before -- an autouse `clean_notifications_stream` fixture in
`conftest.py`, mirroring `clean_payout_stream` exactly. Given this is now
the third time a shared, session-lived Redis Stream has caused this exact
class of bug, any *future* Redis Stream this codebase adds should get the
same autouse-cleanup treatment by default, not as an afterthought once a
test fails.

**Still not built: where this relay's `run_forever()` actually gets
started in a real deployment.** This matches an established, consistent
choice already made for `EngineWorker` and `payout_worker.py` -- this
codebase builds correct, tested runtime primitives (`process_next()` /
`run_forever()` functions and classes) but has never wired any of them
into an actual `main.py`/process-orchestration entrypoint anywhere,
including `services/bot/app.py`'s `build_app()` itself (which assembles
the aiohttp webhook app but is never actually called by anything in this
repo either -- `Notifier.start()`/`.stop()` are likewise only ever called
by test fixtures). Real deployment wiring (which service runs as which
process, systemd unit, or container command) has been consistently out of
scope for this whole session; this relay follows that same line, not a
new gap specific to it.

## 2026-08-24 — Phase 8 (load, chaos, launch readiness — spec Prompt 10 / section 10.3)

**A real, previously-undiscovered money-safety race condition was found and
fixed by this phase's own rush test, before any assertion about seat
allocation was even checked.** `RoundEngine.join()`'s idle-room bootstrap
(`if self._status == "idle": await self._start_new_round()`) had no
synchronization around it. Every prior test that exercised `join()`
against an idle room did so with the first join effectively alone (awaited
before any concurrent ones fired), so this path was never exercised by
genuinely simultaneous callers. `test_load_rush.py`'s 1,000-concurrent-join
scenario fires exactly that: many players hitting a freshly-idle room at
once. Result: multiple coroutines all observed `status == "idle"` before
the first had a chance to flip it, and each tried to `INSERT` the same
next `(room_id, seq)` round row, raising `UniqueViolationError` on
`rounds_room_id_seq_key` — a hard crash, not a graceful rejection, for
what is a completely realistic production scenario (many players arriving
at once when a room has just gone quiet). Fixed with a dedicated
`asyncio.Lock` (`_round_start_lock`, separate from the existing
`_winner_lock` — a different concern) around a double-checked
idle-status read: the outer check keeps the hot "a round is already
running" path lock-free, the inner check-after-acquire ensures only the
first caller among a simultaneous burst actually starts one. Verified at
1,000 concurrent joins against 100 cards afterward: exactly 100 winners,
zero double-allocated cards, elapsed ~2-4s. This is the exact kind of gap
the CTO instructions ask this whole project to catch by actually running
things at real concurrency, not by reading the code and assuming it's
fine — and it's precisely why Prompt 10 exists as its own phase instead of
being treated as optional polish.

**Honest scope gap against the spec's literal target, reported rather than
hidden.** Spec section 10.3 asks for 10,000 concurrent sockets across 200
rooms, sustained for a 30-minute soak, on infrastructure this pass doesn't
have: this environment is a single 4-core / ~8GB dev sandbox where the
load-generating client and the gateway-under-test share the same process
and the same CPU cores, not a distributed load rig hitting a real
multi-replica deployment. Actual measurements taken before settling on the
numbers kept in the committed tests:
- 1 room × 1,000 sockets (pre-existing, `test_gateway_fanout.py`): p99
  ~150-180ms.
- 100 rooms × 10 sockets = 1,000 sockets total
  (`test_load_multiroom.py`, new): p99 ~170-205ms across three separate
  runs — kept as the committed test, spec budget (300ms) comfortably met.
- 100 rooms × 15 sockets = 1,500 total: p99 ~500ms, spec budget exceeded.
- 150 rooms × 20 sockets = 3,000 total: p50 alone ranged from ~430ms to
  ~840ms across two runs — both over budget, and the run-to-run variance
  itself confirms genuine resource contention on this box, not a stable
  measurement.
The honest conclusion: this sandbox's own reliable ceiling for the 300ms
call-to-render budget sits somewhere around 1,000-1,500 concurrent
sockets, not 10,000 — a sandbox/infrastructure limit, not necessarily an
architectural one (the fan-out mechanism itself — one Redis `psubscribe`
per gateway process, bounded per-connection queues — has no obvious
10,000-socket ceiling built into it; verifying that requires a real
multi-replica deployment on real infrastructure this environment doesn't
have, and testing it here would just be measuring this laptop-class
machine, not the architecture). This gap is reported, not
papered over with a lowered assertion pretending to be the real target.

**The "1,000 players rushing one stake tier in 10 seconds" scenario is
exercised at the engine level, not over real WebSockets** (unlike the
fan-out tests) — `RoundEngine.join()` called directly, 1,000 times
concurrently, 10-way contention on every one of only 100 available cards
(deliberately harder than "1,000 players each grab a free card": every
single card has real simultaneous claimants). This isolates the property
actually asked for ("report seat allocation" — correctness under
concurrency) from WebSocket/gateway transport overhead, which the
fan-out tests already cover on their own. Real numbers: exactly 100
winners, 900 `card_taken` rejections, ~2-4s elapsed, zero double-allocated
seats, exactly 100 stakes landed in the pot (not 1,000) — confirmed via
`ledger.reconcile()`-equivalent direct pot-sum assertion.

**Chaos: an engine crash with real concurrent stakes.** `test_worker.py`
already proved crash-recovery works with 2 players; `test_chaos_engine_
crash.py` (new) proves the exact same mechanism holds at 80 real
concurrent players with real money staked (`task.cancel()` simulating a
hard process kill, no graceful shutdown), asserting every one of the 80 is
refunded to the exact centavo, not just that the round ended up voided.

**Chaos: an actual Redis container restart mid-round**, not a mocked
disconnect — `test_chaos_redis_restart.py` (new) really runs `docker
compose restart redis` against the shared dev stack while a round is
`running` with real staked players. Observed, not assumed: the engine's
`run_forever()` genuinely crashes with an unhandled `ConnectionError`
(confirming it does *not* silently and transparently reconnect), and a
fresh `EngineWorker` against the recovered Redis instance correctly finds
the round non-terminal and voids + refunds every player exactly. This is
`packages/core/redis_conn.py`'s own documented promise ("if Redis is
wiped, the platform must recover fully from Postgres") proven against a
real outage instead of just asserted in a docstring. Restarting a
docker-compose service is reversible and scoped to this machine's own dev
stack — the same category of action as the `docker compose down -v`
clean-slate rebuilds already performed after every phase this session,
just narrower (data-preserving vs. destroying volumes).

**Real cross-test pollution found and fixed while building this phase, a
second real bug (test infrastructure, not application code) surfaced by
actually running things together instead of assuming isolation:** the
Redis-restart chaos test was first marked `load` like the others. Running
the full `-m load` batch afterward showed `test_load_rush.py` failing —
not on any of its own assertions (seat allocation was correct every time),
but in its *teardown*, where `engine.stop()` tried to release a room lock
over a Redis connection the earlier chaos test had already pulled the rug
out from under. The session-scoped `redis` fixture is shared across every
test in one pytest process; restarting the real container mid-process
doesn't just affect the test that did it, every fixture built on that
connection is affected for the rest of that process's life. Fixed by
giving container-restarting chaos tests their own marker,
`chaos_infra` (new — registered in `pyproject.toml`, excluded from both
the default run and from `-m load` batches), with the rule spelled out in
the test file's own docstring: always run alone. `test_gateway_fanout.py`
's stalled-reader test failed transiently in the same contaminated batch
run for the same underlying reason, and passed cleanly once the chaos
test was properly isolated.

**Not built this pass, and honestly out of reach without infrastructure
this environment doesn't have:** a real 30-minute soak with a memory
curve across gateway replicas (there is exactly one gateway process here,
not a fleet), and a genuine Postgres-connection-pool-exhaustion chaos
scenario (asyncpg's pool already has a bounded `max_size`; exhausting it
deliberately and confirming graceful backpressure rather than cascading
failure is a real, valuable test that didn't fit in this pass's scope
alongside everything above — a good candidate for a focused follow-up).

## 2026-08-24 — Responsible gaming (spec section 12, the rest of Prompt 9)

Phase 7 (admin console) already built `users.status = 'self_excluded'` and
wired it into deposits. This pass completes the rest of Prompt 9's
explicit test list -- "self-exclusion must block play, deposits, and
marketing" and "a limit increase does not take effect for 24 hours" -- via
a new `packages/core/responsible_gaming.py`, chosen as a `packages/core`
module rather than living inside one service because it's a shared
domain concern three different services need to call into (the engine's
join gate, the payments deposit gate, and the bot's self-service `/limits`
command), the same reasoning `ledger.py`/`bingo.py` already live there.

**Cool-off is purely timestamp-driven, never a `users.status` value.**
`responsible_gaming_limits.cooloff_until` is the sole source of truth;
nothing sets or reads a "cooling_off" status enum, and nothing needs a
scheduled job to lift a cool-off when it expires -- the timestamp
comparison in `check_play_allowed()`/`check_stake_allowed()` naturally
stops blocking the moment `now() >= cooloff_until`.
`test_cool_off_lifts_itself_after_expiry` proves this: a cool-off is set,
its timestamp is directly backdated into the past (no code path exists to
manually "lift" a cool-off, so the test does the only thing an admin or a
cron job restoring from a snapshot could ever do -- wait for time to
pass), and the very next `join()` call succeeds with no other state
change anywhere.

**Self-exclusion, by contrast, deliberately has no lift path at all,
anywhere in this codebase.** `self_exclude()` sets both
`users.status = 'self_excluded'` (the same column every other status
check already reads) and `self_excluded_until` for record-keeping, but
there is no `un_self_exclude()` function, no admin route to clear it, no
duration check gating a reversal. That absence, not a duration check, is
what actually satisfies spec section 12's "irreversible for the period" --
a duration check can be miscalculated or bypassed; a nonexistent function
cannot. `SELF_EXCLUSION_MINIMUM_DAYS = 180` ("6 months minimum") is
enforced as a floor on the way in (`SelfExclusionTooShort` if a caller
asks for less), not on the way out.

**"Blocks registration by the same phone" needed no new code at all** --
`register_from_contact()`'s existing-user lookup is keyed on
`telegram_id`, not phone, so a self-excluded user creating a fresh
Telegram account and trying to register with the same number hits
`phone_e164`'s pre-existing UNIQUE constraint and gets the same
`PhoneAlreadyRegistered` any duplicate-phone registration attempt already
gets. `test_self_excluded_users_phone_cannot_register_a_second_account`
proves the structural guarantee holds; nothing needed to change in
`services/bot/registration.py`.

**The engine's join() gate is deliberately one combined SQL query in the
common case, not three or four separate ones.** The first version of this
wired `check_play_allowed()` (status + cool-off, 2 queries) and
`get_or_create_limits()` (1-3 queries, since it's insert-if-missing) as
two separate calls inside `RoundEngine.join()` -- correct, but it broke
`test_full_round_35_players_ledger_balances` (35 sequential joins against
a 1-second lobby window), because `join()` is a hot path and the added
per-call latency pushed the whole sequence past the lobby timer before
all 35 could join. Not a logic bug, a real performance regression from
adding real work to a hot path. Fixed by adding `check_stake_allowed()`,
one query combining status, cool-off, and the loss-cap fields together
(a plain read-only `SELECT ... LEFT JOIN`, not `get_or_create_limits()`'s
insert-if-missing path, since a missing limits row and an all-NULL one
mean the same thing for a read) -- one round-trip in the common
no-limits-set case, two only when a loss cap is actually configured
(the `today_net_loss()` query only runs then). This is the kind of gap
the CTO instructions ask to catch by actually running things, not just
reading the diff -- caught by the existing test suite immediately, not
discovered later.

**The loss-cap check reuses `today_net_loss()`'s definition of "loss"
literally: stakes debited from `user_cash` today minus payouts credited
to it today.** This deliberately does not distinguish stakes still
in-flight in a live round from ones already settled -- a stake is a real
debit the moment `ledger.post()` commits it, win or lose, so it counts
against the cap immediately, the same way it's already gone from
`user_cash` immediately. A winning stake's payout later nets back against
the same day's total when it settles.

**Bot UX: `/limits` is one command with a subcommand parser, not four
separate commands** (`deposit`/`loss`/`cooloff`/`selfexclude`), matching
the spec's own command table (`/limits` is listed as the single entry
point). This created a real friction point with
`test_bot_no_hardcoded_strings.py`'s AST-based check on
`services/bot/handlers.py`: any inline string comparison like
`if subcommand == "deposit"` is itself a hardcoded literal the checker
(correctly) flags, and locale keys can't be passed as arguments to a
non-`t()` helper either, since the checker only exempts a literal `t(...)`
call's own first argument. Resolved by moving the parsing itself into
`responsible_gaming.parse_limits_command()` (returns a `LimitsAction`
enum member, not a string) and keeping every locale-key selection in
`handlers.py` as a direct `if/else` calling `t("literal.key", ...)`
inline rather than building a key in a variable first -- both patterns
already established this session (`STATUS_APPROVED`/`STATUS_REVIEW` in
Phase 6, the `DepositRejected` exception hierarchy in Phase 5) applied to
a new kind of literal (an enum instead of an exception type).

**Deliberately not built this pass, real open gaps:**
- **Session-time reminders** ("you've been playing 60 minutes") and the
  **reality-check display** on the results screen (net position this
  session). Both are genuinely Mini App frontend + gateway session-tracking
  features, not responsible-gaming domain logic -- Phase 4's screens were
  already reviewed and closed, and adding either is its own frontend pass,
  the same reasoning the Mini App's deposit-amount picker was deferred in
  Phase 5.
- **Age gate / ID verification at KYC level 2.** No identity-verification
  pipeline exists in this codebase (Phase 6 already flagged the same gap
  for withdrawal holder-name matching) -- `kyc_level` is a plain integer
  column an admin would have to set by hand today; there's no real
  verification flow behind it.
- **`marketing_eligible_user_ids()` has no caller.** No bulk/campaign
  notification feature exists anywhere in this codebase yet -- `Notifier
  .send()` is always a reply to one specific user's action, never a
  broadcast. This function exists so that whenever such a feature is
  built, its audience query is already correct and already tested,
  matching the `user:{id}` Redis channel precedent from Phase 3/5 (built
  ahead of need, proven correct in isolation, wired up once a real
  producer/consumer exists).

## 2026-08-24 — Phase 6 (withdrawals, spec Prompt 8 / section 8.3-8.4)

Built the payout side: `services/payments/withdrawals.py` (validation gate,
instant fund-lock, auto-approve rule, admin review queue hooks) and
`services/payments/payout_worker.py` (a real Redis Streams consumer group
-- the first one in this codebase; `services/engine/commands.py`'s own
comment already flagged the payout queue as the place consumer groups
would eventually be needed, unlike the per-room command stream which only
ever has one reader).

**"The single most important step" (the spec's own words) is tested
directly, not just by inference:** `request_withdrawal()` moves
`user_cash -amount` / `user_locked +amount` through `ledger.post()` inside
the same transaction that creates the `payments` row -- there is no
`await` between "decide to lock" and "lock is committed". Proved by
`test_withdrawal_locks_funds_immediately_so_a_later_stake_fails` (a
withdrawal for a user's entire balance, followed by a real
`RoundEngine.join()` attempt, which correctly fails with
`insufficient_funds`) and, for the actually-concurrent case, by
`test_concurrent_withdrawal_and_stake_never_both_succeed` (`asyncio.gather`
on both against the same balance; exactly one succeeds, regardless of
which). Bonus funds are excluded from withdrawals structurally, not by a
conditional check -- `request_withdrawal()` only ever debits `user_cash`,
never `user_bonus`, so `test_bonus_funds_cannot_be_withdrawn` is really
testing that the ledger's own `InsufficientFunds` fires when only
`user_bonus` is funded.

**Exactly-once payout dispatch is the provider's job, not the worker's --
and the worker is written to assume that, not fight it.** Spec: "a crashed
payout worker mid-job → the job is redelivered and the provider is called
exactly once (via our_ref idempotency)." Read literally, a worker cannot
itself guarantee it calls an external API exactly once across a crash --
what it *can* guarantee is passing the same `our_ref` every time, and
trusting the provider to treat that as an idempotency key (this is also
what `chapa.py`'s `create_payout()` already does: `our_ref` goes into
Chapa's own `reference` field). So `payout_worker.process_one()` is safe to
call `provider.create_payout()` again after a redelivery -- it only skips
work for a payment already in a *terminal* state (`succeeded`/`failed`),
never for `processing` (which is exactly the state a crash mid-call would
leave behind).
`test_crashed_worker_job_is_redelivered_and_settles_exactly_once` simulates
the crash directly (forces a payment into `processing`, as if a previous
worker died right after that transition) and confirms the
ledger still settles exactly once (one `ledger_transactions` row for the
`payout-settle-{our_ref}` idempotency key) even though the provider's
`create_payout` genuinely gets called again.

**The consumer group reads each worker's own pending entries (id `"0"`)
before new ones (id `">"`)**, so a worker that restarts under the same
consumer name picks its own abandoned jobs back up without needing
`XCLAIM`/`XAUTOCLAIM` across different consumer identities -- deliberately
the simplest mechanism that satisfies the spec's own test list; reclaiming
a *different* crashed consumer's pending entries (for a multi-replica
deployment where the dead consumer never comes back) is a real gap, noted
below.

**`kyc_level` (0-2) already existed on `users` since Phase 0's own schema**
(present in the original ledger-foundation migration, unused until now) --
no new migration needed for the KYC gate. `MIN_WITHDRAW_ETB` (50 ETB),
`AUTO_APPROVE_WITHDRAW_ETB` (2,000 ETB), and `KYC_REQUIRED_ABOVE_ETB`
(5,000 ETB) are placeholder business parameters, same caveat as Phase 5's
deposit constants -- `idea.md` gives example figures in prose, not
authoritative ones.

**Two anti-fraud rules from spec 8.3's validation gate were deliberately
NOT built, and are real, open gaps, not oversights:**
- **"Payout method holder_name matches account name."** This codebase has
  no independent identity-verification source to check a claimed holder
  name against -- Telegram's `display_name` is self-reported and trivially
  spoofable, so comparing against it would be theater, not a real control.
  Building this for real needs an actual KYC/identity pipeline, which is
  out of scope for this pass. `request_withdrawal()` accepts whatever
  holder name the bot command supplies.
- **"Risk score below threshold" (part of the auto-approve rule).** No
  risk-scoring system exists (spec 8.4's collusion/device-fingerprint/
  velocity flags are all unbuilt -- `risk_flags` was never more than a
  placeholder table name in the spec's own section 4.5). The auto-approve
  rule here checks amount, account age, and lifetime-deposits-vs-
  withdrawals only; every anti-fraud rule in spec 8.4's table is unbuilt.

**Bot UX is deliberately minimal, not the full spec flow.** `/deposit`
already collects only an amount (Phase 5); `/withdraw <amount>
<telebirr number> <full name>` follows the same reasoning -- a single
space-separated command instead of a multi-step aiogram FSM conversation
for choosing a payout method from a saved list. There is no "saved payout
methods" UX; every withdrawal takes a fresh `payment_methods` upsert
(the table itself, and its `(user_id, kind, account_ref)` unique
constraint, already existed from Phase 5's migration). Method kind is
hardcoded to `telebirr` (`withdrawals.DEFAULT_METHOD_KIND`) -- the one
rail every provider in the spec's table covers -- since the bot doesn't
yet ask which rail to use.

**Still deferred from Phase 5, now covering both deposits and
withdrawals:** the Telegram bot confirmation message (spec 8.3 step 8:
"Bot notifies the user with the reason"). `services/payments` remains a
separate process from the bot, and `services/bot/handlers.py`'s own tested
invariant is that nothing calls `bot.send_message` except `Notifier`. This
still needs a small Redis-Streams (or pub/sub) channel the bot process
consumes and forwards through its own `Notifier` -- deliberately not built
this pass either, so it can be done once for both money-movement
directions rather than twice. In the meantime: a depositing/withdrawing
player sees the bot's own synchronous reply to `/deposit`/`/withdraw`
(which does carry real status), the Mini App gets the live
`balance_update` WebSocket push once money actually moves, and the admin
console's audit log and payments queue give full visibility either way.

**Admin review queue additions:** `services/admin/queries.py` gained
`list_pending_withdrawals`, `approve_withdrawal_admin` (audit-logged,
enqueues the real payout job), and `reject_withdrawal_admin`
(ledger-reversed, audit-logged) -- both idempotent no-ops if the payment
isn't in `review` any more, matching `void_round_admin`'s established
pattern. Two new RBAC permissions, `payments:view` (support/finance/ops/
superadmin) and `payments:approve` (finance/superadmin), mirroring the
existing `users:adjust_balance` role split since a withdrawal approval is
exactly that kind of money-movement action.

## 2026-08-22 — Phase 5 (deposits, spec Prompt 7 / section 8.1-8.2)

Built `services/payments/`: a provider-agnostic `PaymentProvider` Protocol
(`provider.py`), a real Chapa adapter (`chapa.py`), the deposit domain logic
(`deposits.py`: create a checkout intent, credit through the ledger on a
verified webhook, a polling fallback, and a pure `reconcile()`), and a
small FastAPI app (`app.py`) exposing the one surface that genuinely has to
be a network endpoint -- the webhook Chapa's own servers POST to.

**No live SantimPay/ArifPay credentials were provided this session** (the
user confirmed having *some* credentials early on but never handed over
actual values), so only the Chapa adapter was built, matching the spec's
own Prompt 7 instruction ("Build ... a Chapa adapter first, structured so
adding SantimPay is a new file and nothing else"). The `PaymentProvider`
Protocol is what makes that literally true: `deposits.py` never imports
`chapa` by name, only the Protocol.

**Chapa's endpoint contract was fetched from developer.chapa.co (2026-08-22)
rather than reconstructed from memory**, since getting a payment
integration's wire format wrong is exactly the kind of mistake that's
invisible until it costs real money: `POST /v1/transaction/initialize` for
checkout creation, `GET /v1/transaction/verify/{tx_ref}` for polling,
`POST /v1/transfers` for payouts, and the two-header webhook signature
scheme (`x-chapa-signature` = HMAC-SHA256 of the raw body, `chapa-signature`
= HMAC-SHA256 of the secret key itself, both keyed with the secret key --
Chapa's own docs: "If either header is missing or the value does not
match, discard the request"). Both are checked in `chapa.py`. The
`/transaction/verify` and `/transfers` *response* body shapes weren't
fully documented in what was fetched; `chapa.py` reads them through
Chapa's standard `{"status", "message", "data"}` envelope, which is
well-established across their whole API, and is not yet verified against a
live sandbox call (no credentials to do that with -- see "not yet tested
against a live rail" below).

**A real gap in Phase 9 (responsible gaming) closed early, deliberately:**
the spec's Prompt 9 wants self-exclusion to block deposits; that couldn't
be built until deposits existed. `create_deposit_intent()` now checks
`users.status = 'self_excluded'` and rejects with `DepositorSelfExcluded`
before ever calling the provider -- covered by
`test_self_excluded_user_cannot_deposit`. The rest of Prompt 9 (loss
limits, cool-off, self-exclusion blocking play and marketing) is still
open.

**`create_deposit_intent()`'s exceptions are a class hierarchy
(`BelowMinimumDeposit`, `DailyDepositCapExceeded`, `DepositorSelfExcluded`,
`UnknownDepositor`, `DepositProviderError`), not one exception with a
string `.reason` field.** Matches the existing
`ContactMismatch`/`InvalidPhone`/`PhoneAlreadyRegistered` pattern in
`services/bot/registration.py`, and sidesteps a real mechanical problem:
`services/bot/handlers.py` has an AST-based test
(`test_bot_no_hardcoded_strings.py`) that fails the build on any hardcoded
string literal in that file, including inside a module-level
`reason -> locale-key` lookup dict. Distinct exception types need no such
table -- `except deposits.BelowMinimumDeposit:` is itself the dispatch.

**`MIN_DEPOSIT_ETB` (10 ETB) and `DAILY_DEPOSIT_CAP_ETB` (50,000 ETB) are
placeholder business parameters** -- `idea.md` never specifies exact
figures for either. Configurable via `Settings`, easy to correct once the
business has real numbers; not a load-bearing design choice.

**Deliberately deferred, not built this pass:**
- **Telegram bot deposit confirmation** (spec step 9: "Bot notification:
  '✅ 200 ETB deposited...'"). The locale key (`notify.deposit_confirmed`)
  already existed from an earlier phase, unused. What's missing is the
  cross-process path: `services/payments` is a separate FastAPI process
  from the bot, and `services/bot/handlers.py`'s own docstring makes
  "nothing calls `bot.send_message` except `Notifier`" a load-bearing,
  tested invariant -- so this needs a small Redis-Streams
  notify-the-bot-process channel, not a direct call. Withdrawals (Phase 6)
  need the exact same capability ("Bot notifies the user with the reason"),
  so building it once for both is the plan rather than a rushed half now.
  In the meantime, a depositing player still gets instant confirmation via
  the live `balance_update` WebSocket push (below) if they're in the Mini
  App, and can always check `/balance`.
- **The Mini App's own deposit-amount UI** (spec's `[+]` button on the
  wallet screen). The bot's `/deposit <amount>` command is the complete,
  real entry point this pass; Phase 4's Mini App screens were already
  reviewed and closed, and adding a full amount-picker screen there is a
  frontend feature addition warranting its own pass rather than a
  backend-phase add-on.
- **Live testing against Chapa's actual sandbox.** Every test in
  `tests/unit/test_chapa_provider.py` and
  `tests/integration/test_payments_deposits.py` exercises the real
  business logic (idempotency, row-locking, ledger crediting, amount
  verification) against a `FakePaymentProvider` test double standing in
  for Chapa's network -- the same reasoning every other test in this suite
  uses a real local Postgres/Redis/gateway instead of a live external
  dependency it can't safely or repeatably call. `chapa.py` itself has
  never made a real network call. **This is the one honest gap in this
  phase:** the adapter is built correctly against Chapa's documented
  contract, but has not been proven against Chapa's own server, because no
  real API key was available this session. That verification should happen
  before any real money moves through it.

**New in this phase, verified real (not test-only) end to end:** the
`user:{id}` Redis pub/sub channel that `services/gateway/fanout.py` already
subscribed to since Phase 3 -- built ahead of need, unused until now -- is
what a deposit's ledger credit now publishes to
(`deposits._publish_balance_update`), and `web/miniapp/js/app.js` now has a
`balance_update` handler that updates the header and, if open, the wallet
screen live. `test_webhook_pushes_live_balance_update_over_websocket`
proves the whole chain: a real HTTP POST to `services/payments/app.py`'s
webhook route credits the ledger and a real connected WebSocket receives
the push, with no mocking anywhere in that path.

## 2026-08-22 — Phase 7 (admin console, spec section 33) — a real gap found while wiring the fairness-verification route

Built the admin API as a fully separate FastAPI app (`services/admin/app.py`,
distinct process/port from the player-facing gateway): username + bcrypt
password + TOTP 2FA login, Redis-backed session tokens (not JWT, so a
session is server-side revocable on logout — matters for an admin console
more than for players), RBAC via a single `has_permission(role, permission)`
function checked on every mutating and every viewing route, and an
append-only audit log enforced at the database level (`BEFORE UPDATE OR
DELETE` trigger that raises rather than a convention nobody can verify).
Every mutation that touches money (`adjust_balance`, `void_round_admin`)
goes through `packages/core/ledger.py` like any other money movement —
no route in `services/admin/app.py` ever writes a balance directly.

**Real bug found, not a test bug:** building `get_round_fairness()` (the
route a regulator or a suspicious player would use to verify a finished
round's draw was not rigged) surfaced that `services/engine/round_engine.py`
never actually persisted the revealed `server_seed` to the `rounds` table —
only `server_seed_hash` (written at round start, for the commit half of the
commit-reveal scheme) and `draw_order` (written when the round starts
running) were ever saved. The actual `server_seed` bytes lived only in the
engine's in-memory state and were broadcast once over Redis pub/sub in the
`round_end` message — an ephemeral, unsubscribed-and-it's-gone channel, not
a queryable record. That made independent, after-the-fact fairness
verification structurally impossible for any round nobody happened to be
connected for at the exact moment it ended, which defeats the entire point
of a provably-fair scheme (the "provable" part requires the proof to still
exist later). Fixed by persisting `server_seed` to `rounds` in both terminal
paths: `_settle_with_winners()` (winner found) and the exhausted/no-winner
refund path in `_run_calling_phase()`. Caught by writing
`test_round_fairness_verification_matches_a_real_round`, which failed with
`fairness["revealed"] is False` against a round that had genuinely
finished — not a fixture/timing bug, the column was just always NULL.

Two other issues fixed in `tests/integration/test_admin_queries.py` while
building this, both narrow test bugs rather than app bugs (documented per
the same discipline as Phase 3/4's test-vs-app-bug calls):
- `test_search_users_finds_by_phone_fragment` hardcoded the literal phone
  `+251911223344`, which collided with `phone_e164`'s UNIQUE constraint on
  any second run against the same long-lived test database. Added
  `unique_phone()` to `tests/integration/conftest.py` (same counter pattern
  already used for `next_telegram_id()`, and already existed independently,
  duplicated, in `test_bot_handlers.py`) and switched this test to it.
- Three `RoundEngine`-backed tests used `lobby_seconds=30` with a 10s
  `asyncio.wait_for(task, timeout=10)` on `engine.stop()`. This is a real,
  narrow characteristic of `_run_lobby()` (it doesn't poll
  `self._stop_requested` inside its wait, so `stop()` can't interrupt a long
  lobby wait promptly) — deliberately not "fixed" in the engine itself,
  since a slow-to-cancel lobby wait is harmless in production and every
  other engine test already uses short `lobby_seconds` for exactly this
  reason. Brought these three into line instead (`lobby_seconds=5`).

Also: `asyncpg` returns `jsonb` columns as raw JSON text with no codec
registered (true everywhere else in this codebase too) — a test asserting
into `admin_audit_log.before`/`after` needed `json.loads()` first. Noted as
a possible future global fix (register a `jsonb` → `dict` codec on the pool
once, instead of every call site doing it ad hoc) but not made this pass —
touching pool-wide codec config this late in a session isn't worth the risk
for a cosmetic convenience.

Wrote `tests/integration/test_admin_app.py` for the HTTP layer specifically
(real `uvicorn` server via a new `admin_server` fixture mirroring the
existing `gateway_server` one — genuine requests through the actual
FastAPI dependency chain, not RBAC asserted by calling `has_permission()`
directly): login end-to-end, bearer-token rejection, RBAC enforced per-role
over real HTTP (`support` blocked from `/users/{id}/adjust`, `finance`
allowed and money actually moves), audit-log route restricted to
`superadmin`, logout actually invalidating the session, IP allowlist
returning 403 for a disallowed source, and the audit log's immutability
trigger firing on a direct `UPDATE`/`DELETE` attempt. All ran green first
try against a from-scratch `docker compose down -v` rebuild.

## 2026-08-22 — Phase 4 (Mini App, spec's Mini App UI Specification) — real bugs found by actually testing in a browser

This phase is the reason the CTO instructions insist on testing UI in a
real browser before calling it done — every one of these was invisible to
`mypy`/unit tests and would have shipped silently broken:

- **`ws.js`'s auth resolver passed the wrong object.** `authResolvers`
  were resolved with the whole `{t, user, server_time}` envelope instead
  of `message.user`, so every caller's `user.balance` read `undefined`.
  The WebSocket traffic looked perfect in isolation (`authed` arrived with
  a real balance) -- only reading the actual DOM (`#balance-amount`)
  caught it. Fixed to resolve with `message.user`, matching
  `waitForAuth()`'s other branch.
- **Taking a card never fetched the card's actual grid.** `state_sync`
  only includes `your_card_grid` once `round_entries` has a row for that
  user; the LOBBY-time `state_sync` (before taking a card) naturally has
  it `null`. The `take_card` ack handler patched `your_card` locally but
  never re-fetched state, so `your_card_grid` stayed `null` -- crashing
  `setCardGrid()` the moment `round_start` fired (`TypeError: Cannot read
  properties of null (reading '0')`, only visible in a running browser's
  console, never in a `curl`/WS-trace-level check). Fixed by re-sending
  `join` after a successful `take_card` ack, reusing the existing
  `state_sync` path rather than inventing a new one.
- **`round_start`'s stat-strip update used the wrong object.** The handler
  correctly merged `round_start`'s payload into `state.round` for storage,
  but then called `updateStatStrip(sync)` with the *raw*, unmerged
  `round_start` payload -- which has no `stake` field (only `state_sync`
  does) -- leaving the stake stat blank after the first `round_start` of a
  session. Same fix also closed a second bug in the same code path:
  `state.round.called` was never actually reset to `[]` in the global
  store across rounds (only a local copy was), which would have let a
  stale called-number from a previous round leak into the *next* round's
  client-side pattern check. Caught by reviewing an actual screenshot from
  the E2E test, not by any assertion. One merged object now, reused for
  `setState`, `enterGame`/`enterSpectate`, and `updateStatStrip` alike.
- **The E2E test's own `page.route()` handler was a no-op.**
  `lambda route: route.abort()` creates the coroutine and never awaits or
  schedules it -- a classic Python async mistake, silently doing nothing.
  Without it, index.html's real `<script src="https://telegram.org/js/
  telegram-web-app.js">` loads after the test's `add_init_script` stub and
  overwrites `window.Telegram.WebApp.initData` back to `""`, breaking auth
  in a way that looked like an app bug until traced to the test harness
  itself. Fixed with a real `async def` handler.
- **A real double-claim race, found only by running the E2E test enough
  times to hit it**: a single player can produce two independent valid
  claims for the same round through two separate paths -- the server's own
  AUTO-mode scan (`_call_next_number`) and a client-sent `claim` message
  (the Mini App's client-side AUTO toggle independently detects the same
  completed pattern and sends its own claim). Both could land in
  `_pending_winners`, crashing `round_winners`' `(round_id, user_id)`
  primary key at settlement -- `asyncpg.exceptions.UniqueViolationError`,
  visible only as an intermittent `finally: await asyncio.wait_for(task,
  ...)` failure in whichever test happened to run the engine long enough
  to hit it (roughly 1 in 3 runs of the gameplay E2E test). Fixed in
  `round_engine.py`'s `claim()`: reject a second claim from a user_id
  already in `_pending_winners`, regardless of source (`"already_claimed"`)
  -- one claim per user per round, full stop. Added a direct regression
  test (`test_same_user_double_claim_race_settles_exactly_once`) that
  fires both claim sources concurrently for the same user without needing
  a browser to reproduce it.
- **The E2E gameplay test still has residual, lower-rate flakiness**
  (~1 in 7-8 runs, after the fix above) waiting for the game screen to
  appear -- a plain timeout, no crash, no console error, and it always
  passes on retry. This matches the same category already documented for
  the load tests: five to eight consecutive heavy Playwright+uvicorn+engine
  test invocations on one shared 4-core dev box will occasionally hit real
  resource contention. Bumped the wait budget as cheap mitigation (15s →
  25s) and documenting rather than chasing further, since it shows no
  reproducible error signal to chase and the test is opt-in
  (`pytest -m e2e`), not part of the default suite or any CI gate.
- **The E2E gameplay test was itself flaky** (~2/3 failure rate) for an
  unrelated reason: the dev database accumulates rooms across every prior
  test run at the same 10.00 ETB stake, and `page.click(".room-card")`
  clicked whichever one sorted first -- non-deterministic against ties,
  so it sometimes hit some other, already-finished room whose
  `state_sync` never reports `status: "lobby"`. Fixed by adding
  `data-room-id` to each room card in `renderRoomList()` and having the
  test target its own room specifically. Confirmed with 4 consecutive
  clean runs afterward.
- **`#screen-rooms` used to default to `class="screen active"` in the raw
  HTML** (so something was visible before JS ran) and the balance
  placeholder used to be `"0.00 ETB"` -- both made the *first* E2E test
  draft pass regardless of whether auth had actually succeeded, since a
  real `"0.00 ETB"` balance is indistinguishable from the placeholder. No
  screen defaults to active now; the loading placeholder is `"-- ETB"`, so
  a genuine `"0.00 ETB"` is unambiguous proof `authed` arrived.

**Design decisions**, not bugs:

- **Amharic font is subsetted to the codepoints actually used, not the
  full Ethiopic block.** Google's full "ethiopic" delivery is ~200KB, over
  4x the spec's 40KB budget (Ethiopic is a syllabary -- hundreds of glyphs,
  not a ~26-letter alphabet). Subsetting to the ~122 characters currently
  used across `web/miniapp/locales/am.json` and the bot's `am`/`ti`
  locales gets it to ~11.5KB. Tradeoff: a new Amharic string introducing a
  character outside the current subset falls back to a system font for
  that glyph until `fonts/subset.sh` is rerun -- documented in the script
  itself and in `css/fonts.css`'s own comment, not just here.
- **Gateway REST endpoints (`/api/me`, `/api/history`) added for the
  wallet screen**, not just the WebSocket protocol. The spec's client→
  server message list (§6.2) has no request for balance snapshot or
  history; rather than force those through the realtime channel, they're
  plain authenticated REST calls (`Authorization: tma <initData>`, same
  `validate_init_data` boundary as the WS handshake) -- a normal read
  doesn't need a persistent connection, and this matches the spec's own
  architecture diagram showing "REST API" as a sibling to "WebSocket",
  not something WebSocket-only.
- **`state_sync` extended** with `stake`, `win_patterns`, `players`,
  `derash`, `your_card_grid`, and `lobby_deadline_ms` -- Phase 3's version
  only had what the (not-yet-built) Mini App's actual needs could reveal.
  All sourced from Postgres, consistent with Phase 3's existing "state_sync
  never depends on a live engine" principle.
- **Mini App static files served by the gateway itself**
  (`StaticFiles` mount on `/`), anchored to `services/gateway/app.py`'s own
  file location rather than the process's cwd. Simplest path for local
  dev/testing and a legitimate (if not final) production option; a real
  deployment would likely put these behind a CDN instead, which is a pure
  infra change with no code impact.
- **Playwright is a dev-only, on-demand dependency** (`pytest -m e2e`,
  same pattern as the load tests), not part of the default suite --
  a Chromium binary is a heavy, environment-specific thing to require for
  every contributor's default `pytest tests/` run.

---

## 2026-08-22 — Phase 1 (Telegram bot, spec Prompt 5) scoping decisions

- **`/deposit`, `/withdraw`, `/limits` honestly report "not available yet"**
  rather than faking a payment or limits flow. Building a working-looking
  deposit command with no real provider behind it (Phase 5-6, not built) or
  a limits command with no responsible-gambling engine behind it (Phase 7,
  not built) would be exactly the "fake/mock functionality" the CTO
  instructions explicitly forbid. `/balance`, `/history`, `/invite`,
  `/language`, `/rules`, `/support`, and the full registration flow are all
  real, reading and writing the actual ledger/DB.
- **Play button is conditional on `MINIAPP_URL` being configured.** Telegram
  requires a valid HTTPS URL for a `web_app` button; there is no Mini App
  yet (Phase 4). Rather than ship a button that would error or point
  nowhere, `/play` and the main menu button fall back to an honest "not
  open yet" message until `MINIAPP_URL` is set.
- **Referral codes are just the referrer's own numeric user_id** (`ref_
  {telegram_id}`), not a separately generated/looked-up code — already
  unique, no new table needed. Only the `referred_by` link and a plain
  count are implemented; the `referrals` table's reward/qualifying-deposit
  tracking (spec section 4.5) depends on deposits existing, so it's
  deferred to whichever phase builds the promotions engine.
- **`/change_username` takes its argument inline** (`/change_username New
  Name`) rather than using aiogram's FSM (a "send the command, then reply
  with the name" conversation). Avoids pulling in FSM storage machinery for
  one simple field; can be upgraded to a real conversation later if product
  wants a friendlier flow.
- **Structural, AST-based test enforcing "no hardcoded string in any
  handler"** (`tests/unit/test_bot_no_hardcoded_strings.py`), not just
  reviewer discipline. It walks the whole `handlers.py` AST for string
  literals with real alphabetic content and fails on anything not
  specifically exempted (a `t(...)` call's translation key, a
  `Command(...)`/`Router(...)` protocol identifier, a `row["key"]`
  Record-subscript, a ledger account "kind" enum value, or an f-string's
  structural fragments used only for URL building). Every exemption was
  added only after the checker actually flagged that real, verified-safe
  case against the live file — none were guessed in advance. The checker
  caught one real bug while being built: `outcome = "won" if ... else "—"`
  was hardcoded English text passed straight into a `t(...)` format kwarg,
  bypassing i18n entirely; fixed by adding `history.outcome_won` /
  `history.outcome_other` translation keys.
- **aiogram's `Router` can only attach to one `Dispatcher` for its
  lifetime.** `services/bot/handlers.py`'s `router` is the normal aiogram
  module-level-singleton pattern, correct for production (one long-lived
  Dispatcher), but it meant test code building a fresh `Dispatcher` per
  test function crashed on the second test ("router already attached").
  Fixed by sharing one session-scoped `Dispatcher`/`Bot`/fake-session
  across all of `test_bot_handlers.py`, matching how the bot actually runs.
- **Bot API testing uses a fake `aiogram.client.session.base.BaseSession`
  subclass** that records `SendMessage` calls and returns a synthetic
  `Message` instead of touching the network — no real bot token or
  Telegram connectivity needed. Combined with `Dispatcher.feed_update()`
  fed synthetic `Update` objects, this exercises the real Router/handler
  code end to end, which is what let the spec's own Prompt 5 test list
  (contact-mismatch rejection, typed-number rejection, duplicate
  `update_id`) be proven directly rather than approximated.
- **Amharic/English translations are a first-pass draft**, not
  native-speaker-reviewed — except the handful of strings taken verbatim
  from the spec itself (`register.prompt`, `register.use_button`,
  `wallet.insufficient`), which are presumably already vetted. Flagging
  this explicitly rather than implying production-ready translation
  quality; a native speaker should review `services/bot/locales/am.json`
  before this ships for real users.
- **Docker containers had stopped mid-session** (host uptime showed an
  11-minute restart partway through this phase) — not a code issue,
  just `docker compose up -d postgres redis` again, data intact via the
  named volume. Noted because it produced a wall of unrelated-looking
  connection-refused test failures that could otherwise look like a real
  regression.
- **Load-test variance reconfirmed, more dramatically.** Immediately after
  the container restart above (host uptime 11 minutes, other unrelated
  Docker workloads on the same shared 4-core machine also mid-restart),
  the 1000-socket fan-out test measured p99 = 482-528ms across repeated
  runs -- well over budget. Waiting and rerunning once the host settled
  brought it straight back to 147-190ms, matching every other measurement
  taken during this project. Real, environment-driven variance on shared
  infrastructure, not a regression in the (unchanged) fan-out code --
  exactly why this test is `@pytest.mark.load` and excluded from the
  default run rather than a hard CI gate.

---

## 2026-08-22 — Phase 3 (realtime gateway, spec Prompt 4) scoping decisions

- **Gateway↔engine command channel: Redis Streams, one per room.** Prompt 3
  deliberately left this open (engine tested as a direct Python API). Built
  it now as `services/engine/commands.py`: the gateway `XADD`s a command
  with a correlation id and awaits a reply on a per-request pubsub channel;
  `RoundEngine._serve_commands()` is the sole consumer of its own room's
  stream for as long as it holds the room lock, so no consumer group is
  needed the way the (not-yet-built) payout queue will need one. A command
  to a room with no live owner times out cleanly (`CommandTimeout`) rather
  than hanging.
- **`state_sync` and `rooms` are served straight from Postgres**, not
  through the engine. Postgres is already the durable source of truth
  (`round_engine.py` writes every transition there before publishing
  anything), so reconnection works correctly even mid-crash-recovery, with
  no dependency on a live engine existing right now. Only the four
  money/state-moving actions (`join`/`drop_card`/`claim`/`set_auto`) go
  over the command channel.
- **Backpressure is queue-depth-based, not byte-size-based.** The spec's
  "64 KB send buffer" language assumes access to the raw socket's write
  buffer, which Starlette/ASGI doesn't expose at the application level. Each
  connection gets a bounded `asyncio.Queue` (`fanout.MAX_QUEUE_SIZE = 100`)
  instead; when it's full, pending droppable messages (`lobby_tick`, `call`)
  are dropped and a `state_sync` is queued next, matching the spec's actual
  behavioral intent (a slow client falls back to a full resync rather than
  blocking the room) via a different but equivalent signal.
- **`round_end`'s per-winner shape carries over from Phase 2** (see that
  section below) — a list of `{user_id, card_no, pattern, amount}` rather
  than one top-level `pattern`/`cells` pair, since ties can have co-winners
  on different patterns.
- **Two protocol additions beyond the spec's literal table**, both noted
  here rather than silently invented: a `rooms` reply (`{"t": "rooms",
  "rooms": [...]}`) for the `rooms` request — the spec names the request
  but not its reply shape — and a generic `{"t": "ack", "for": ..., "ok":
  ..., "reason": ...}` for `take_card`/`drop_card`/`set_auto`, since the
  spec only names a distinct reply (`claim_result`) for `claim`. Clients
  need *some* way to know their own action's own result distinctly from the
  room's broadcast of it.
- **`take_card`'s client-supplied `idem` field is accepted but unused.**
  `RoundEngine.join()` already derives its own idempotency key from
  `(round_id, user_id)`, which is a stronger guarantee than a client-chosen
  token (immune to a client reusing or mis-generating one) — the field is
  read from the frame for protocol compatibility and otherwise ignored.
- **1000-socket fan-out test is marked `@pytest.mark.load`, excluded from
  the default `pytest tests/` run.** Measured p99 in isolation: ~130–175ms
  (well under the spec's 300ms budget), repeatedly. Run as the 423rd test
  in one long-lived pytest process (everything else's accumulated GC
  pressure and event-loop bookkeeping sharing the same 4 cores as the test
  itself), the same scenario measured p99 = 318ms — confirmed by rerunning
  it standalone immediately afterward, which came back at 167ms, proving
  the regression was process-level contention, not architecture. This is
  exactly why real load tests run as their own dedicated process rather
  than interleaved with a correctness suite; `pytest -m load` runs it
  separately for a clean reading. Numbers are what this single 4-core dev
  machine produced, not a production claim — Phase 8's real load testing
  runs against a real deployed topology with load generators on separate
  hardware from the server, which is the only way to responsibly measure
  the spec's 10,000-concurrent target.
- **`get_or_create_user_by_telegram_id` lazily creates a user row on first
  Mini App connect.** In the target architecture the bot (Phase 1, not yet
  built) creates the user during registration before the Mini App is ever
  opened. This bridges the gap until then and becomes a pure no-op read
  once Phase 1 exists.
- **Fixed a real bug found while writing gateway tests**: `ledger.balance()`
  and `ledger.available()` returned an unscaled `Decimal("0")` (displays as
  `"0"`) for an account with no history yet, instead of `Decimal("0.00")`.
  Both now `.quantize(Decimal("0.01"))` before returning — money always
  renders at 2 decimal places, full stop.

---

## 2026-08-22 — Phase 2 (game engine, spec Prompt 3) scoping decisions

- **Round status: `voided` covers every refund-only outcome.** The spec's
  `rounds.status` enum (`lobby|running|settling|done|voided`) doesn't
  distinguish "ran to completion with no winner" from "never really
  happened." Using `voided` uniformly for lobby-underfill, 75-calls-exhausted-
  no-winner, *and* crash recovery means all three share one function
  (`services/engine/refunds.py::refund_round`) instead of three near-copies
  of the same ledger logic. `done` is reserved exclusively for a round that
  actually paid a winner.
- **Stakes debit `user_cash` only.** The spec's `available()` balance
  (`user_cash + user_bonus - user_locked`) implies bonus funds can stake, but
  there's no wagering-requirement tracking built yet (no prompt in the pack
  covers bonuses specifically — it's mentioned under Payments §8.5, Prompt
  7/8 territory). Staking from bonus without anything to track wagering
  progress would be a half-feature, so `join()` only ever touches
  `user_cash`; revisit when the bonus/promotion engine is built.
- **`_publish_room()` implements the room-broadcast events only**
  (`lobby_tick`, `round_start`, `card_taken`, `call`, `round_end`) — not the
  per-user `claim_result` / `balance` messages from spec §6.3. Those are
  responses to a specific connection's action, which is Prompt 4's (the
  gateway's) concept to own once it exists; `claim()`'s return value already
  carries everything a caller needs. Publishing to `user:{id}` for a balance
  push with nothing subscribed yet would be speculative.
- **`round_end`'s `winners` is a list of `{user_id, card_no, pattern,
  amount}`**, not the single top-level `pattern`/`cells` fields the spec's
  table sketches (written assuming one winner). A tied round can have
  co-winners on different patterns; per-winner fields are the honest
  representation.
- **The gateway↔engine command transport is out of scope for Prompt 3.**
  `RoundEngine` exposes `join`/`drop_card`/`claim` as a plain async Python
  API, matching how Prompt 3's own test list exercises it (direct
  simulation, not over a network). How a WebSocket message reaches a
  same-room engine running in a different process (Redis Stream per room is
  the natural fit, matching the payout-queue pattern the spec already uses
  elsewhere) is Prompt 4's problem to solve.
- **`max_players` has a narrow TOCTOU race for non-default room sizes.** The
  check happens in memory before the atomic DB insert. The hard invariant —
  no duplicate card, no double-charge — is still fully enforced by the
  `(round_id, card_no)` primary key regardless, since the card pool caps at
  100 and most rooms will set `max_players = 100` anyway. Noted rather than
  fixed with a distributed counter, which felt like solving a problem this
  phase doesn't actually have yet.
- **`RoomLock`'s TTL and refresh interval are constructor-configurable**
  (defaults still 15s / 5s per spec) purely so the test suite can prove
  expiry/refresh behavior in ~1.5s instead of 15s+, without changing
  production timing.
- Discovered `services/__init__.py` was missing (Phase 0 created every
  `services/<name>/__init__.py` but not the parent package marker) while
  wiring engine imports — mypy caught it immediately as a duplicate-module
  error. Added.

---

## 2026-08-22 — `idea.md` contains three layered documents; adopted the "Jo Bingo" spec pack as authoritative

`idea.md` turned out to be three documents concatenated, not one:

1. A generic "Bengo" CTO-prompt (lines 1–2175) — a requirements checklist,
   suggesting a generic Node/Go + React stack.
2. A second, more detailed pass of the same checklist, "Bengo — Extreme
   Production Build" (lines 2176–4775).
3. The **"Jo Bingo" spec pack** (lines 4776–6298) — concrete and internally
   consistent: exact SQL schema, exact economics (35 players × 20 ETB stake
   → 700 pot → 560 derash at a 20% house cut), exact WebSocket protocol,
   exact provider list (Chapa/SantimPay/ArifPay), exact stack (Python 3.12,
   FastAPI, aiogram 3, PostgreSQL 15, Redis 7, asyncpg, Alembic, pytest), and
   its own 9-prompt build sequence with test requirements per prompt.

**Decision:** the Jo Bingo spec pack is the authoritative blueprint (stack,
schema, economics, protocol). The two generic sections are treated as the
requirement checklist Jo Bingo must satisfy — compliance, security,
observability etc. get layered on as the corresponding Jo Bingo phase is
built, not invented ahead of schedule. Confirmed with the user before
writing any code.

## 2026-08-22 — Credentials not collected yet

User has a Telegram bot token and a SantimPay/ArifPay key already, but
neither is needed until Phase 1 (bot) or Phase 5–6 (payments). Not collected
during Phase 0 — `.env.example` documents the variable names so `config.py`
has a stable shape now; real values go directly into a local `.env` (never
committed) when those phases start.

## 2026-08-22 — Delivery is phase by phase

Per the spec's own instruction ("one prompt per session... stop after each
numbered prompt for review") and the sheer size of the system (10 build
phases, ~16 weeks by the spec's own estimate), work proceeds one phase at a
time, each reviewed before the next starts. Phase 0 (this pass) covers the
spec's Prompt 1 (foundations + ledger) and Prompt 2 (pure bingo logic).

## 2026-08-22 — `account_balances` denormalizes `kind` from `accounts`

The spec's own SQL sketch (§4.2) doesn't put `kind` on `account_balances`,
but a real non-negative-balance guarantee for `user_cash` / `user_bonus` /
`user_locked` needs a CHECK constraint, and Postgres CHECK constraints can't
reference another table. Copying `kind` onto `account_balances` at row
creation (immutable thereafter) makes `CHECK (kind NOT IN (...) OR balance
>= 0)` a real, table-local, database-enforced constraint instead of
something only `ledger.post()`'s row-lock logic protects. Belt and
suspenders: the row lock is still what makes concurrent debits correct; the
CHECK is the backstop if anything ever writes to `account_balances` outside
`post()`.

## 2026-08-22 — Partial unique index for system accounts

`accounts` has `UNIQUE (user_id, kind, currency)`, but Postgres treats
`NULL` as distinct from `NULL` in a unique constraint — so that constraint
alone would let two `house_revenue` rows (both `user_id = NULL`) coexist.
Added `CREATE UNIQUE INDEX ... ON accounts (kind, currency) WHERE user_id IS
NULL` so system accounts (`house_revenue`, `pot_escrow`, etc.) stay
singletons the same way user accounts do.

## 2026-08-22 — Postgres/Redis moved off default ports in docker-compose

This machine already runs another project's Postgres (5432) and Redis
(6379). `deploy/docker-compose.yml` maps Jo Bingo's containers to host ports
5433 and 6380 instead; `packages/core/config.py` and `.env.example` default
to those. Purely a local-dev accommodation, has no bearing on production
deployment.

## Pre-existing repo state noted, not touched

This directory already had a `.git` folder with `origin` pointing to
`https://github.com/Nebyudejenie/game.git` before this session started (no
commits yet). Left as-is — not part of this work, and remotes/commits are
outside what was asked for.

## Observed: an "Initial commit" appeared and was pushed to origin/main mid-session, not made by the assistant

While Phase 0 files were being written, `git log` came to show a commit
("Initial commit", author `Nebyu Dejenie <nebyudejenie1@gmail.com>` — the
account's own configured git identity, matching `origin`) containing that
work, and `refs/remotes/origin/main` recorded `update by push` at the same
timestamp. The assistant issued no `git add`, `git commit`, or `git push` in
this session (project instructions require an explicit ask before
committing, and none was given) — most likely explanation is the user
staging/committing/pushing via VS Code's Source Control panel while working
alongside this session. Content pushed matches what was verified with a
green test suite, so noted here rather than acted on.
