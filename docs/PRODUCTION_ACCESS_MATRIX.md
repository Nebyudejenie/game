# Production Access Matrix

What's actually public, what's actually private, and how each is
enforced — verified directly against the live server and real external
requests on 2026-09-05, not assumed from the compose files alone. See
`docs/PRODUCTION_DOMAIN_AND_CLOUDFLARE.md` for the routing architecture
and current tunnel-config status this table describes, and
`docs/TELEBIRR_ROLES_AND_ACCESS.md` for the full RBAC permission-to-role
mapping.

## Public surface

| Hostname | Service | Public today | Auth | WebSocket |
|---|---|:---:|---|:---:|
| `arada.fun`, `www.arada.fun` | gateway (Mini App + player REST API) | YES | Telegram `initData` HMAC (per-request) | YES (`/ws`, verified live) |
| `arada.fun/webhook` | bot (Telegram webhook) | YES | Telegram webhook secret | NO |
| `payments.arada.fun` | payments (MacroDroid ingestion, Chapa webhook, `/internal/telebirr/ingest`) | YES | Bearer token (`hmac.compare_digest`) for ingestion; provider-signature check for the Chapa webhook | NO |
| `agent.arada.fun` | payments (Agent Portal — same container as above) | YES | Opaque Redis session, obtained via a one-time Telegram-delivered link | NO |
| `admin.arada.fun` | admin console | YES | Username + password + TOTP → bearer session token, plus `ADMIN_IP_ALLOWLIST` (confirmed live: a non-allowlisted external request gets a real `403 "source IP not permitted"`) | NO |
| `finance.arada.fun` | admin console (finance-role login, same container) | YES | Same as admin | NO |

All five hostnames verified with real external requests: missing/wrong
bearer token on payments → `401`, malformed ingest body → `422`,
unauthenticated Agent Portal API → `401`, admin/finance IP-allowlist →
`403` for this session's own non-allowlisted IP.

`api.arada.fun` was not created: `gateway` already serves `/api/*` at
`arada.fun` itself.

## Explicitly NOT public — verified, not assumed

| Component | Reachable from the internet? | How verified |
|---|:---:|---|
| PostgreSQL (`jobingo-postgres`) | **NO** | `docker ps` shows no host port mapping at all (bare `5432/tcp`, not `0.0.0.0:5432` or even `127.0.0.1:5432`) |
| Redis (`jobingo-redis`) | **NO** | Same — bare `6379/tcp`, no host binding |
| Docker daemon / socket | **NO** | Never exposed on any host port in any compose file inspected |
| SSH (port 22) | **NO** via the public domain | The Cloudflare Tunnel's ingress list has only the hostname rules above (all → Traefik on port 80) plus a catch-all 404 — SSH is a separate, direct LAN connection to the host's own port 22, never routed through Cloudflare at all |
| Engine-worker / payout-worker | **NO** | No `ports:` mapping, no Traefik label — internal-only, `/metrics` scraped by Prometheus on the internal network only |
| `payments`'s own `/metrics` | **NO** | The new `Host(payments.arada.fun)` Traefik rule covers the whole service, but `/metrics` has no auth of its own — deliberately left this way since it's low-sensitivity (counts, not values, per `packages/core/metrics.py`), matching directive guidance to avoid inventing unnecessary hardening; flagged here for visibility, not hidden |
| `admin`'s own `/metrics`, if it has one | N/A | Admin has no `/metrics` route to worry about |

## Admin console: now exposed, publicly live

Previously not exposed because its IP-allowlist check
(`services/admin/app.py::_client_ip`) trusted only the raw TCP connection
IP — which becomes Traefik's own address the instant this service sits
behind a reverse proxy, silently breaking `ADMIN_IP_ALLOWLIST` if it's
ever configured. Fixed this session: `_client_ip` now trusts Cloudflare's
own `CF-Connecting-IP` header (set at Cloudflare's edge, unforgeable by a
client the way `X-Forwarded-For` would be) when present, falling back to
the raw connection IP unchanged for direct/SSH-tunnel access. Verified
with a new test (`test_ip_allowlist_trusts_cf_connecting_ip_over_the_raw_
connection_ip`): an allowed `CF-Connecting-IP` passes even when the raw
test-client IP itself was never allowlisted, and a disallowed one is
still blocked.

`admin.arada.fun` and `finance.arada.fun` are both live publicly now,
confirmed with a real external request: a request without a matching
`CF-Connecting-IP` gets a real `403 "source IP not permitted"`, proving
the allowlist survives the full Cloudflare → Tunnel → Traefik path
intact, not silently bypassed.

## RBAC: enforced at the API, not just hidden in the UI

Verified directly: a request with no `Authorization` header, and
separately one with a garbage bearer token, both against `/dashboard` and
against the superadmin-only `/audit-log`, returned `403` in every case
sent straight to the admin container. The full permission-to-role table
(`payments:view`, `payments:view_raw_evidence`, `payments:approve`,
`payments:configure`, `audit:view`) is unchanged from
`docs/TELEBIRR_ROLES_AND_ACCESS.md`.

The Agent Portal's own authorization was verified the same way (real
requests, not code-reading alone): a session belonging to agent A cannot
see agent B's submissions or any MacroDroid-sourced evidence; a
deactivated agent's already-issued session stops working on its very
next request, not after its TTL expires; every submissions-list response
was checked field-by-field to confirm `raw_sms`/payer/recipient
information is never present.

## Secrets

- `MACRODROID_INGEST_TOKEN`: correctly set in the real `.env`. Never
  printed anywhere during the fix.
- `PHONE_ENCRYPTION_KEY`: production's real value is confirmed (by hash
  comparison only, values never printed) to be **different** from the one
  committed to git history in `.env.example` — production is not using
  the exposed value. The exposed value remains in git history; see the
  domain doc's "remaining gaps" for what that does and doesn't affect.
- `payment_provider_availability.telebirr_sms` remains `enabled = false`
  in production, confirmed by direct query — the whole feature is wired
  and reachable end-to-end but the player-facing redemption path stays
  off until an explicit decision to flip it on, after a real controlled
  SMS test succeeds.
