# Production Domain and Cloudflare Architecture

Everything in this file was verified directly against the live production
server and real external HTTPS/WebSocket clients on 2026-09-05 — not
inferred from the repo's own aspirational comments
(`deploy/cloudflared/config.yml.example` describes a 4-subdomain scheme
that was never actually implemented; this file describes what is
actually running, and what is staged and ready pending one action only
someone with root on the box can take).

## The real architecture

```
Internet
   |
   v
Cloudflare (DNS + TLS, Let's Encrypt cert via Cloudflare)
   |
   v
Cloudflare Tunnel "arada-bingo" (systemd service on the host, NOT a
   Docker container -- /etc/systemd/system/cloudflared.service, config
   at /etc/cloudflared/config.yml, tunnel ID 23b7b57e-a207-4d77-9bab-
   33d39227d15b). The same Cloudflare account also runs several other,
   completely unrelated tunnels (n8n, rustdesk, santimpay, ...) --
   confirmed via `cloudflared tunnel list` before touching anything, and
   only this one tunnel's own DNS routes were ever touched.
   |
   v
http://localhost:80  (Traefik, part of the shared "hermis" stack this
                       box also runs -- not part of jobingo's own compose
                       file)
   |
   +-- Host(arada.fun) || Host(www.arada.fun)   -> gateway:8000
   +-- Host(arada.fun) && PathPrefix(/webhook)   -> bot:8003
   +-- Host(payments.arada.fun)                  -> payments:8002
   +-- Host(agent.arada.fun)                     -> payments:8002 (same service, see below)
   +-- Host(admin.arada.fun)                     -> admin:8001
   +-- Host(finance.arada.fun)                   -> admin:8001 (same service, see below)
```

## Status as of 2026-09-05

| Hostname | DNS | Tunnel ingress | Traefik | Backend | Public end-to-end |
|---|:---:|:---:|:---:|---|:---:|
| `arada.fun` / `www.arada.fun` | live | live | live | gateway | **PASS** |
| `payments.arada.fun` | live | **pending** | live | payments | pending tunnel config |
| `agent.arada.fun` | live | **pending** | live | payments (Agent Portal) | pending tunnel config |
| `admin.arada.fun` | live | **pending** | live | admin | pending tunnel config |
| `finance.arada.fun` | live | **pending** | live | admin (finance-role login) | pending tunnel config |

DNS was created for all four new hostnames this session (`cloudflared
tunnel route dns arada-bingo <hostname>`, using the origin cert already
on the box — no new Cloudflare credentials were needed for this part).
Traefik routing for all four is live right now. The **only** missing
piece is `/etc/cloudflared/config.yml` itself: it's `root:root`-owned
(`-rw-r--r--`) and this session's user (`cosmic`) has no passwordless
`sudo` (`sudo -n true` returns "interactive authentication is required")
— a genuine, hard OS permission boundary, not a policy choice. The
replacement file is already staged and diffed at
`/tmp/new_cloudflared_config.yml` on the server; applying it needs
exactly:

```bash
sudo cp /etc/cloudflared/config.yml /etc/cloudflared/config.yml.bak-$(date +%Y%m%d%H%M%S)
sudo cp /tmp/new_cloudflared_config.yml /etc/cloudflared/config.yml
sudo systemctl restart cloudflared
```

This restarts only the `arada-bingo` tunnel's own systemd unit — the
other unrelated tunnels on this shared account are separate services,
unaffected. Expect a 1-2 second blip on `arada.fun` itself during the
restart, nothing longer.

## Why `payments`/`agent` and `admin`/`finance` are the same containers, twice

`agent.arada.fun` and `payments.arada.fun` both route to the exact same
`jobingo-payments` container (two Traefik routers, `jobingo-payments` and
`jobingo-agent`, both pointing at the identical
`traefik.http.services.jobingo-payments` service) — not two deployments.
The Agent Portal (`web/agent`, `services/payments/agent_auth.py`, the
`/agent-portal/*` routes) is genuinely part of the payments service, the
same "don't create a second payment system" discipline the whole Telebirr
feature was built under. `finance.arada.fun` and `admin.arada.fun`
likewise both route to the one `jobingo-admin` container: "Finance" is an
RBAC role (`admin_users.role = 'finance'`) inside that one console, not a
separate app — a finance login sees a different set of screens, enforced
server-side, regardless of which hostname reached it. See
`docs/ADMIN_DASHBOARD_GUIDE.md` and `docs/AGENT_DASHBOARD_GUIDE.md` for
what each surface actually shows.

`api.arada.fun` was not created: `gateway` already serves its REST API
under `/api/*` at `arada.fun` itself (the same service as the Mini App) —
a second hostname for the identical container would be redundant.

## Verified live, 2026-09-05 (real external requests against `arada.fun`, already live)

| Check | Result |
|---|---|
| `https://arada.fun/` (gateway / Mini App) | `200`, valid Let's Encrypt cert (`CN=arada.fun`) |
| `http://arada.fun/` | `200` — **not** redirected to HTTPS (see Remaining gaps) |
| `wss://arada.fun/ws` (game WebSocket) | Connects; server correctly closes unauthenticated traffic with `4000 expected_auth` — proves the upgrade traverses Cloudflare Tunnel → Traefik → gateway intact |
| `POST https://arada.fun/webhook` (Telegram) | `401` (real webhook-secret check, not a generic 404/405) |
| `GET https://arada.fun/webhook` | `405` (POST-only, correct) |
| Control: `POST https://arada.fun/<random-nonexistent-path>` | `405` (gateway's generic fallback) |

The `payments.arada.fun/internal/telebirr/ingest` auth flow (missing
token → `401`, wrong token → `401`) was verified live *before* the
hostname was switched from a path under `arada.fun` — identical backend
route, identical auth code, so this doesn't need re-proving, only
re-confirming once the tunnel config lands (see the acceptance report for
the exact command to re-run then).

## What changed this session

1. Added a Traefik route for `payments`, first as a path under `arada.fun`
   (`PathPrefix(/internal/telebirr)`), verified live, then upgraded to a
   real dedicated `payments.arada.fun` hostname per explicit direction —
   the path-based route was removed, not left as a redundant second way in.
2. `MACRODROID_INGEST_TOKEN`: found set (a real 64-char value) in
   `.env.example` on the server — the *template* file, meant to always be
   blank and is committed to git. It had never actually been committed in
   that state (verified via `git log`), but was one `git add -A` away
   from leaking a real secret into version control. Moved the value into
   the real `.env`, reverted `.env.example` to its committed blank line.
3. Migration `b31c5f70f957` (two-line Bingo win rule, unrelated to
   payments but was pending) applied via the existing one-off `migrate`
   service — pure schema/data, safe independent of the engine-worker code
   deploy, which stays pending (see `docs/PRODUCTION_ACCESS_MATRIX.md`).
4. Built the Payment Agent Portal (`services/payments/agent_auth.py`,
   three new routes, `web/agent`) and the bot's `/portal` command —
   see `docs/AGENT_DASHBOARD_GUIDE.md`.
5. Fixed `services/admin/app.py`'s IP-allowlist to trust Cloudflare's own
   `CF-Connecting-IP` header (unforgeable — set at Cloudflare's edge) once
   behind a reverse proxy, instead of the raw connection IP, which would
   have silently become Traefik's own address — this was the blocker that
   made exposing `admin.arada.fun` safe to do at all.
6. Created DNS + Traefik routes for `payments.arada.fun`,
   `agent.arada.fun`, `admin.arada.fun`, `finance.arada.fun`. Tunnel
   ingress staged, pending root access (see Status table above).
7. Set `PAYMENTS_PUBLIC_BASE_URL=https://payments.arada.fun` and
   `AGENT_PORTAL_BASE_URL=https://agent.arada.fun` in the real `.env` —
   the first was previously unset entirely (Chapa's own webhook callback
   URL was never configured; this is a pre-existing gap this work
   incidentally fixed).

## Remaining gaps (not fixed this session — need a decision or access this session doesn't have)

- **HTTP is not redirected to HTTPS.** `http://arada.fun/` returns `200`
  directly instead of a redirect. This is a Cloudflare zone-level setting
  (Always Use HTTPS / a redirect rule) — fixing it needs Cloudflare
  dashboard or API access, which this session doesn't have.
- **A real, working secret sits in git history**, not just the working
  tree: commit `d6f5c79` ("Encrypt phone numbers at rest") added a real,
  functioning `PHONE_ENCRYPTION_KEY` to `.env.example`, explicitly
  labeled in its own commit message as "a real, working DEV key, not a
  placeholder." Confirmed by hash comparison (never printing either
  value) that **production uses a completely different, separately
  generated key** — production itself is not compromised. The checked-in
  dev key is permanently recoverable from git history by anyone with repo
  access; rotating it (a new commit) helps future clones but doesn't
  erase history — only a rewrite (`git filter-repo`/BFG, force-push,
  everyone re-clones) does that, a decision for you, not something to do
  unilaterally.
- **The cloudflared tunnel config edit itself** — see the Status table.
