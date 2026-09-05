# Production Domain and Cloudflare Architecture

Everything in this file was verified directly against the live production
server and a real external HTTPS client on 2026-09-05 — not inferred from
the repo's own aspirational comments (`deploy/cloudflared/config.yml.example`
describes a 4-subdomain scheme that was never actually implemented; this
file describes what is actually running).

## The real, live architecture

```
Internet
   |
   v
Cloudflare (DNS + TLS, Let's Encrypt cert via Cloudflare)
   |
   v
Cloudflare Tunnel (systemd service on the host, NOT a Docker container --
   /etc/systemd/system/cloudflared.service, config at
   /etc/cloudflared/config.yml)
   |
   v
http://localhost:80  (Traefik, part of the shared "hermis" stack this
                       box also runs -- not part of jobingo's own compose
                       file)
   |
   +-- Host(arada.fun) || Host(www.arada.fun)              -> gateway:8000
   +-- Host(arada.fun) && PathPrefix(/webhook)              -> bot:8003
   +-- Host(arada.fun) && PathPrefix(/internal/telebirr)    -> payments:8002
```

**One tunnel, two hostnames, that's it.** The live `/etc/cloudflared/config.yml`
ingress list is exactly:

```yaml
ingress:
  - hostname: arada.fun
    service: http://localhost:80
  - hostname: www.arada.fun
    service: http://localhost:80
  - service: http_status:404
```

No `admin.`, `payments.`, `pay.`, `bot.`, `api.`, `finance.`, or
`agent.arada.fun` DNS record or tunnel ingress rule exists. Every public
route lives under the one tunneled hostname, disambiguated by Traefik's
own `PathPrefix` matcher — the same pattern the `bot` service's webhook
route already used before this work, now extended to `payments`.

## Why path-based routing under `arada.fun`, not new subdomains

A CTO-level request for this system asked for `admin.arada.fun`,
`finance.arada.fun`, `agent.arada.fun`, `api.arada.fun`, and
`payments.arada.fun` as literal separate subdomains. After inspecting the
live code and infrastructure (not assuming), most of those don't
correspond to a real, separate thing to route to:

- **`payments.arada.fun`**: reused the existing tunneled `arada.fun`
  hostname with a Traefik `PathPrefix` instead, exactly mirroring how
  `bot`'s own webhook route already works. A literal new subdomain would
  require editing the systemd-level tunnel config and adding a real DNS
  record — technically possible, but unnecessary when the path-based
  approach reaches the identical destination with zero new public
  attack surface and zero new infrastructure to maintain. The resulting
  URL is `https://arada.fun/internal/telebirr/ingest` — confirmed live.
- **`admin.arada.fun`**: the admin console (`services/admin` + `web/admin`)
  currently has **no public route of any kind** — it's bound to
  `127.0.0.1:8001` on the host only. Exposing it requires a real decision
  (see `docs/PRODUCTION_ACCESS_MATRIX.md`) because unlike `bot`/`payments`,
  its ~15 API routes aren't under one path prefix, and its IP-allowlist
  security check (`services/admin/app.py::_check_ip_allowlist`) reads the
  raw TCP connection IP, which would silently see only Traefik's own IP
  if placed behind it — a real security regression if `ADMIN_IP_ALLOWLIST`
  is ever configured, unless `X-Forwarded-For` handling is added first.
  **Not exposed publicly** — this matches the explicit decision made this
  session to verify the admin console is built and working without
  changing its current (SSH-tunnel-only) access model.
- **`finance.arada.fun` / `agent.arada.fun`**: these are not separate
  backend services. "Finance" is an RBAC role (`admin_users.role =
  'finance'`) inside the *same single* admin console app — a finance user
  logs into the identical console at whatever hostname it's served from
  and sees a different set of menu items/permissions, enforced server-side
  (`services/admin/rbac.py`), not a different deployment. "Agent" isn't an
  admin role at all: a Payment Agent's only interface is the private
  Telegram bot (`services/bot/handlers.py`'s `_is_active_payment_agent`
  filter) — there is no agent-facing web app in this codebase to route to.
  Creating either subdomain would mean routing to the exact same admin
  container a second time under a different name (all downside, no new
  capability), or standing up a brand-new duplicate system that doesn't
  exist today — both of which contradict the "no second wallet, no second
  ledger" discipline this whole payment feature was already built under.
- **`api.arada.fun`**: `gateway` already serves its REST API under
  `/api/*` at the existing `arada.fun` hostname (the same service that
  serves the Mini App itself). A separate `api.arada.fun` pointing at the
  identical container would be two public hostnames for one service with
  no functional difference — not created.

## Verified live, 2026-09-05 (real external HTTPS requests, not local tests)

| Check | Result |
|---|---|
| `https://arada.fun/` (gateway / Mini App) | `200`, valid Let's Encrypt cert (`CN=arada.fun`) |
| `http://arada.fun/` | `200` — **not** redirected to HTTPS (see Remaining gaps below) |
| `wss://arada.fun/ws` (game WebSocket) | Connects; server correctly closes unauthenticated traffic with `4000 expected_auth` — proves the upgrade traverses Cloudflare Tunnel → Traefik → gateway intact |
| `POST https://arada.fun/webhook` (Telegram) | `401` (real webhook-secret check, not a generic 404/405) |
| `GET https://arada.fun/webhook` | `405` (POST-only, correct) |
| `POST https://arada.fun/internal/telebirr/ingest`, no auth | `401 missing bearer token` |
| same, wrong token | `401 invalid bearer token` |
| same, before this session's fix | `405` (fell through to gateway's catch-all — the route didn't exist) |
| Control: `POST https://arada.fun/<random-nonexistent-path>` | `405` (gateway's generic fallback — confirms the telebirr 401s above are the real payments service responding, not a coincidence) |

## What changed this session

1. `deploy/docker-compose.yml` (on the server, not this git repo — that
   file's real, hand-maintained instance lives at
   `/home/cosmic/jo-bingo/deploy/docker-compose.yml`): added a
   `traefik.*` label block to the `payments` service, scoped to
   `PathPrefix(/internal/telebirr)` only — `/metrics` and any other
   payments route stay unreachable from outside. `payments` container
   recreated to pick it up.
2. `MACRODROID_INGEST_TOKEN`: found set (a real 64-char value) in
   `.env.example` on the server — the *template* file, meant to always be
   blank and is committed to git. It had never been committed in that
   state (verified: `git status` showed it as a local, uncommitted
   modification only), but was one `git add -A` away from leaking a real
   secret into version control. Moved the value into the real `.env`
   (gitignored, correct place for it), reverted `.env.example` back to
   its committed blank line. `payments` recreated again to load it.
3. Migration `b31c5f70f957` (two-line Bingo win rule, unrelated to
   payments but was pending) applied via the existing one-off `migrate`
   service — pure schema/data, safe independent of any code deploy.

## Remaining gaps (not fixed this session — need a decision or access this session doesn't have)

- **HTTP is not redirected to HTTPS.** `http://arada.fun/` returns `200`
  directly instead of a redirect. This is a Cloudflare zone-level setting
  (Always Use HTTPS / a redirect rule) — fixing it needs Cloudflare
  dashboard or API access, which this session doesn't have. See the
  REQUIRED block in the final report.
- **A real, working secret sits in git history**, not just the working
  tree: commit `d6f5c79` ("Encrypt phone numbers at rest") added a real,
  functioning `PHONE_ENCRYPTION_KEY` to `.env.example`, explicitly
  labeled in its own commit message as "a real, working DEV key, not a
  placeholder." Confirmed by hash comparison (never printing either
  value) that **production uses a completely different, separately
  generated key** — production itself is not compromised. But the
  checked-in dev key is permanently recoverable from git history by
  anyone with repo access, and would silently protect zero confidentiality
  for any real data a local/dev/staging environment ever encrypts with
  it. Rotating the checked-in value (a new commit) reduces exposure for
  future clones but doesn't erase history; only a history rewrite
  (`git filter-repo`/BFG, force-push, everyone re-clones) removes it
  entirely — a decision for you, not something to do unilaterally.
- **Admin console has no public route.** Deliberate, per this session's
  own explicit decision — see `docs/PRODUCTION_ACCESS_MATRIX.md`.
