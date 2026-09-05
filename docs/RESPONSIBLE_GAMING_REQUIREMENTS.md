# Responsible Gaming Requirements

Separates what the platform **technically does** (verifiable in code,
today) from what the **law requires** (jurisdiction-specific, not yet
reviewed by counsel). The TECHNICAL columns below are populated from
direct code review. The LEGAL columns are left explicitly **UNKNOWN** —
not "assumed fine," not "probably compliant" — until a qualified local
regulator/counsel review happens (`docs/LAUNCH_BLOCKERS.md` LB-D2). Never
treat a populated TECHNICAL column as evidence the LEGAL column is
satisfied; they are independent questions.

| Technical control | Current implementation | Evidence | Legal requirement | Legal source | Status |
|---|---|---|---|---|---|
| Age verification | A declared 18+ confirmation at registration, recorded server-side and timestamped. | `users.age_confirmed_at` (migration `d812e3d87349_age_gate_declaration`), set via `COALESCE(age_confirmed_at, now())` the moment registration first completes (`services/bot/registration.py:26-27,95,166`). This is a **declaration**, not third-party identity/age verification (no document check, no KYC provider integrated for age specifically). | UNKNOWN | UNKNOWN | UNKNOWN |
| Self-exclusion | A player can self-exclude via `/limits selfexclude confirm`; enforced through `users.status = 'self_excluded'`, the same status column every other access check already reads. Deliberately **irreversible for the exclusion period** — there is no "lift my own self-exclusion" function anywhere in the codebase; only the passage of time (`self_excluded_until`) restores access, and even then requires no additional action to *not* self-exclude (no reactivation asked of the platform). | `packages/core/responsible_gaming.py::self_exclude()`; `SELF_EXCLUSION_MINIMUM_DAYS = 180` (comment: "spec section 12: '6 months minimum'"). Enforced at play (`check_play_allowed()`) and at deposit (`services/payments/deposits.py`'s eligibility check) and excluded from all marketing (`marketing_eligible_user_ids()`, wired into the Notification Center's audience resolution this session — see `PRODUCTION_READINESS.md` fix #4). | UNKNOWN (is 180 days the legally required minimum in the actual operating jurisdiction, or a product guess?) | UNKNOWN | UNKNOWN |
| Cool-off (temporary break) | A player can set a temporary cooling-off period (`24h`/`7d`/`30d` presets via `/limits cooloff <duration>`). Purely timestamp-driven (`cooloff_until`) — lifts itself automatically the moment the timestamp passes, no scheduled job needed. | `packages/core/responsible_gaming.py::cool_off()`, `COOLOFF_DURATIONS_HOURS`. Enforced at play and excluded from marketing, same mechanism as self-exclusion. | UNKNOWN | UNKNOWN | UNKNOWN |
| Deposit limits | A player can set a daily deposit cap. A **decrease takes effect immediately**; an **increase is delayed 24 hours** before taking effect — a deliberate friction against impulsive limit-raising during a losing session. | `packages/core/responsible_gaming.py::set_deposit_limit()`, `effective_deposit_cap()`, `LIMIT_INCREASE_DELAY_HOURS = 24`. Enforced server-side in the deposit eligibility check (`services/payments/deposits.py`), not just displayed client-side — confirmed by this pass's own passing `test_deposit_daily_cap_uses_the_ethiopian_calendar_day_not_utc` test, which proves a bypass attempt via a manipulated timestamp still fails. | UNKNOWN (is an instant-decrease/24h-increase-delay shape a real regulatory requirement, or a product design choice made without one?) | UNKNOWN | UNKNOWN |
| Loss limits | Same shape as deposit limits (instant decrease, 24h delayed increase) applied to a rolling daily net-loss figure. | `packages/core/responsible_gaming.py::set_loss_limit()`, `effective_loss_cap()`, `today_net_loss()` (reads real `stake` ledger entries, EAT-calendar-day-aware — confirmed and fixed this pass, see `PRODUCTION_READINESS.md` fix #7). Enforced via `check_stake_allowed()` before any stake is accepted. | UNKNOWN | UNKNOWN | UNKNOWN |
| Marketing suppression for at-risk players | Self-excluded, banned, or currently-cooling-off users are unconditionally excluded from every promotional/marketing send, not overridable by any campaign's audience filter. | `packages/core/campaigns.py::_build_where()` (fixed this session — see `PRODUCTION_READINESS.md` fix #4), mirroring the pre-existing, tested `packages/core/responsible_gaming.py::marketing_eligible_user_ids()` whose own docstring states it is "the one query any future marketing/promotional send must use." | UNKNOWN (many jurisdictions mandate this; some go further — e.g., banning any "win-back" targeting of self-excluded players specifically) | UNKNOWN | UNKNOWN |
| Reality check / session-time reminders | Implemented, client-side: a toast reminder at 60/120/180 minutes of session time ("You've been playing for {minutes} minutes"), plus a running net win/loss total shown to the player at each round's result screen. Explicitly a plain awareness nudge, not a server-enforced control (unlike the five controls above) — resets on reload by design. | `web/miniapp/js/app.v6.js` (`SESSION_REMINDER_MINUTES = [60, 120, 180]`, `checkSessionReminder()`; `sessionNetPosition` tracked and surfaced at `round_end`); `web/miniapp/locales/en.json:129` (`"session.reminder"`). | UNKNOWN (some regulated markets require a *server-side*, non-resettable reality check with mandatory acknowledgment, not just a client-side toast) | UNKNOWN | UNKNOWN — real control exists; whether its specific shape (client-side, resets on reload, no forced acknowledgment) meets the actual legal bar needs review |
| Deposit/loss limit floor (a maximum the platform itself enforces, not just player-chosen) | **Not found as a separate, platform-enforced maximum** independent of what a player sets for themselves — the mechanism supports arbitrary player-chosen caps, but there's no evidence of a platform-wide ceiling a player cannot exceed even if they try to set a very high limit. | N/A | UNKNOWN (some jurisdictions mandate an absolute maximum stake/deposit regardless of player preference) | UNKNOWN | **Gap — needs a product decision once legal requirement is known** |
| Underage-access prevention beyond self-declaration | **Not implemented** — the age gate is a self-declaration with no independent verification (no ID check, no third-party age-verification service integrated). | See age verification row above. | UNKNOWN (many regulated gambling jurisdictions require more than self-declaration) | UNKNOWN | **Gap — needs a product decision once legal requirement is known** |
| Problem-gambling resource links (helpline, support org) | **Not found** — no in-bot or in-Mini-App reference to a gambling-support helpline or resource was located. | Grep-confirmed absence. | UNKNOWN (commonly mandated alongside self-exclusion features) | UNKNOWN | **Gap — needs a product decision once legal requirement is known** |

## What this document is not

This is not a legal compliance certification. Every UNKNOWN and every
"Gap" row above needs a real answer from qualified local counsel/a
regulator for the actual jurisdiction(s) this product will operate in
(`docs/LAUNCH_BLOCKERS.md` LB-D2) before any of them can be marked
resolved. Building more technical controls without first knowing what's
actually required risks solving the wrong problem — the three "Gap" rows
above are flagged as candidates for follow-up work, not committed to,
pending that legal input.

## What is solid regardless of the legal answer

The five implemented controls (age declaration, self-exclusion, cool-off,
deposit limits, loss limits) share one property worth stating plainly:
every one of them is enforced **server-side**, at the actual point of
action (play, stake, deposit, marketing send) — not merely surfaced as a
UI suggestion a determined player could route around. This is a real,
verified engineering property independent of which specific numbers or
mechanisms a legal review ultimately requires.
