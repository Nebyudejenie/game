# Telegram Security Audit

A dedicated audit of this platform's single biggest attack surface: a
real-money product whose only client identity comes from Telegram's
`initData`, not a password or session cookie. Built by reading the actual
code, not by re-describing intent — every claim below cites the file and
line it comes from.

## 1. initData validation

**Mechanism**: `packages/core/telegram_auth.py::validate_init_data()`.

- Parses `initData` as a query string, extracts and removes `hash`.
- Rebuilds the data-check string per Telegram's own spec (remaining
  fields, sorted, `k=v` joined by `\n`).
- `secret_key = HMAC-SHA256(key="WebAppData", message=BOT_TOKEN)`,
  `computed = HMAC-SHA256(key=secret_key, message=data_check_string)`.
- Compares `computed` to the provided hash with `hmac.compare_digest()`
  — constant-time, so a timing side-channel can't leak the valid hash
  byte by byte (`telegram_auth.py:82`).
- **Freshness**: rejects if `auth_date` is more than 24h old
  (`DEFAULT_MAX_AGE_SECONDS`), and rejects a *future*-dated `auth_date`
  beyond 60 seconds of clock skew (`telegram_auth.py:91-94`) — a captured
  `initData` (logged by a proxy, stuck in browser history, shared in a
  screenshot) cannot become a permanent session token.
- Every failure path raises a specific, distinctly-named
  `InvalidInitData` reason (`empty`, `missing_hash`, `bad_hash`,
  `stale_auth_date`, `auth_date_in_future`, `missing_user`,
  `malformed_user`) — the module's own docstring is explicit that callers
  must treat every reason identically (reject the connection); the
  distinct reasons exist for logging/testing, never for different
  client-facing behavior. Confirmed both call sites do exactly this (no
  reason-specific branching found in `connection.py` or `app.py`).

**Both entry points share this one function, not two parallel
implementations**:
- WebSocket: `services/gateway/connection.py:128`.
- REST: `services/gateway/app.py:108` (comment at line 101 explicitly
  notes "same `validate_init_data()` boundary, same rules").

There is no third, undiscovered auth path — grepped for any other
`hmac`/`hash` comparison involving a bot token anywhere in `services/` or
`packages/`; none found outside this one module.

## 2. Replay protection

The 24-hour freshness window (above) is the primary defense — an
intercepted `initData` string is a live credential only within that
window, not forever. There is no *additional* single-use/nonce tracking
(Telegram's own `initData` isn't a one-time token by design; the freshness
window is the standard mitigation Telegram's own documentation
recommends). Residual risk: a captured, still-fresh `initData` (e.g., via
a compromised proxy or a malicious browser extension) is replayable
*within* that 24h window. This is a known, inherent property of
Telegram's `initData` design, not a gap specific to this codebase — no
mitigation beyond a shorter window is available without diverging from
Telegram's own auth contract. Not a launch blocker; noted for awareness.

## 3. Session handling after initial auth

- **WebSocket**: `self._user_id` (`connection.py:71`) is set exactly
  once, at `handle_auth()` time (`connection.py:140`), from the server's
  own `get_or_create_user_by_telegram_id()` lookup keyed on the
  *validated* Telegram user id — never from a client-supplied user id
  field. Every subsequent handler in the same connection
  (`join`/`drop_card`/`claim`/`set_auto`/state-sync/rate-limiting) reads
  this same server-set `self._user_id` (confirmed via direct grep: 12
  distinct uses, all reading `self._user_id`, zero reading any per-message
  client field for identity). A single WS connection cannot impersonate a
  different user mid-session — there is no message shape that re-derives
  identity from client input after the initial handshake.
- **REST (admin/finance/agent consoles)**: separate from the Mini App's
  Telegram-based auth entirely — opaque bearer tokens in Redis, live
  `is_active` re-check on every request (not just at login), covered in
  `docs/PRODUCTION_READINESS.md`'s Security section. Not duplicated here.

## 4. The server never trusts client-provided money-relevant fields

Checked directly, not assumed:

- **User identity**: see #3 above — always server-derived from the
  authenticated Telegram id, never from client payload.
- **Balance**: every balance shown to a client comes from
  `packages/core/ledger.py::user_balance_snapshot()`/`balance()`, a
  server-side read of `account_balances`; no WS/REST handler accepts a
  client-supplied balance value for anything.
- **Card ownership**: `queries.held_card_no_for_room()`/
  `held_card_no_for_round()` (`connection.py:241,283`) look up which card
  a user actually holds from the database, keyed by the server-resolved
  `user_id` — a client cannot claim a card it doesn't actually hold by
  simply naming a different `card_no` in a `claim` message; the engine's
  own `join()`/`claim()` re-validate card ownership against `self._entries`
  server-side (`round_engine.py`), independent of whatever the client
  claims.
- **Claim status / win determination**: `RoundEngine.claim()` computes
  the actual win/loss result server-side from the room's own drawn-number
  history and the card's real grid (`packages/core/bingo.py`); the client
  never supplies "I won" — it only supplies "I am claiming a win for this
  card," and the server is the sole authority on whether that's true.
- **Payout amount**: settlement (`services/engine/round_engine.py`'s
  settlement path, `packages/core/ledger.py::post()`) computes payout
  splits from the room's own recorded pot and stake, never from any
  client-supplied amount.
- **Wallet mutation amounts (deposits/withdrawals)**: every ledger-posting
  path takes its amount from the *server's own* record (Chapa's webhook
  payload verified against a stored expected amount, an admin's own
  approval-form input for manual flows, Telebirr's own parsed SMS
  evidence) — never a bare client-supplied number accepted at face value.
  This is the same invariant `docs/PRODUCTION_READINESS.md`'s wallet/
  ledger rows already establish; restated here in Telegram-specific terms
  because it's the property this audit exists to confirm end to end.

## 5. WebSocket-specific concerns

- **Origin validation**: no explicit `Origin` header check found in
  `connection.py` — the WS endpoint accepts a handshake from any origin
  that can reach it. This is mitigated, not eliminated, by the fact that
  a connection is functionally useless without a valid, freshly-signed
  `initData` (which only Telegram's own client can produce, tied to the
  real bot token) — an attacker controlling an arbitrary origin still
  cannot forge a valid `auth` message. Residual risk is limited to
  cross-origin *connection noise* (an attacker's page can open a WS
  connection and immediately have it rejected at the `auth` step), not
  impersonation. Not a launch blocker.
- **Rate limiting**: real Redis Lua token bucket, confirmed to cover WS
  actions (`connection.py:333-337`), fails closed on a Redis error
  (`docs/PRODUCTION_READINESS.md`'s own citation) — a WS client cannot
  flood the server with unlimited join/claim/drop_card messages.
- **CSRF/CORS**: not applicable to the WS/Mini-App surface the same way
  it isn't applicable to the admin consoles — the Mini App's only
  meaningful action (`auth`) requires a real, HMAC-signed `initData` no
  cross-origin page can produce; there is no ambient-cookie-based auth
  for CSRF to exploit anywhere in this codebase.

## 6. What this audit did not, and could not, verify from this environment

- **Real Telegram WebView behavior**: this audit confirms the *code's*
  handling of `initData`; it cannot confirm real-world Telegram client
  behavior (does a real Telegram client always deliver a genuinely fresh
  `auth_date`? does backgrounding/foregrounding the Mini App trigger a
  fresh `initData`, or reuse a stale one past the 24h window?). See
  `docs/LAUNCH_BLOCKERS.md`'s LB-C1 (real-world Telegram/Mini App smoke
  test) — this is a physical-device verification item, not a code one.
- **Bot token compromise blast radius**: not modeled here as a separate
  scenario; a leaked bot token would let an attacker forge valid
  `initData` for any Telegram user id of their choosing. Standard
  mitigation (rotate the token, `BotFather` revocation) is an operational
  procedure, not a code fix — out of scope for this document.

## Summary

| Control | Status |
|---|---|
| initData HMAC validation | GREEN — real, constant-time, single shared implementation |
| Freshness/replay window | GREEN — 24h max age, 60s future-skew tolerance |
| Session identity binding | GREEN — server-derived, never client-supplied, confirmed at every use site |
| Balance/card/claim/payout trust boundary | GREEN — server is sole authority at every step checked |
| WS origin validation | YELLOW — no explicit check, but exploitation requires forging `initData`, which origin alone cannot do |
| Real-device/WebView behavior | UNVERIFIED — requires a physical device (LB-C1) |
| Bot token compromise procedure | Out of scope — operational, not code |
