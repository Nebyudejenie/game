# Production Access Matrix

What's actually public, what's actually private, and how each is
enforced — verified directly against the live server and real external
requests on 2026-09-05, not assumed from the compose files alone. See
`docs/PRODUCTION_DOMAIN_AND_CLOUDFLARE.md` for the routing architecture
this table describes, and `docs/TELEBIRR_ROLES_AND_ACCESS.md` for the
full RBAC permission-to-role mapping.

## Public surface

| Hostname/path | Service | Public | Auth | WebSocket |
|---|---|:---:|---|:---:|
| `arada.fun`, `www.arada.fun` | gateway (Mini App + player REST API) | YES | Telegram `initData` HMAC (per-request) | YES (`/ws`, verified live) |
| `arada.fun/webhook` | bot (Telegram webhook) | YES | Telegram webhook secret | NO |
| `arada.fun/internal/telebirr` | payments (MacroDroid SMS ingestion) | YES | Bearer token, `hmac.compare_digest` | NO |
| `arada.fun` (rest of the path space) | — | — | falls through to gateway's own routing, generic `405`/`404` for anything gateway doesn't define | — |

No `admin.arada.fun`, `finance.arada.fun`, `agent.arada.fun`,
`api.arada.fun`, or `payments.arada.fun` DNS/tunnel entry exists. See the
domain doc for why each was deliberately not created.

## Explicitly NOT public — verified, not assumed

| Component | Reachable from the internet? | How verified |
|---|:---:|---|
| PostgreSQL (`jobingo-postgres`) | **NO** | `docker ps` shows no host port mapping at all for this project's own DB container (bare `5432/tcp`, not `0.0.0.0:5432` or even `127.0.0.1:5432`) |
| Redis (`jobingo-redis`) | **NO** | Same — bare `6379/tcp`, no host binding |
| Admin console (`jobingo-admin`) | **NO** | Bound to `127.0.0.1:8001` (host loopback only); zero Traefik label; confirmed no matching ingress rule in the live cloudflared config |
| Payments service's own bare port | **NO** (only the one specific path is) | Bound to `127.0.0.1:8002`; only reachable publicly via the new Traefik `PathPrefix(/internal/telebirr)` rule — `/metrics`, `/healthz`, and `/webhooks/chapa` on that same container are not covered by any public route today |
| Docker daemon / socket | **NO** | Never exposed on any host port in any compose file inspected |
| SSH (port 22) | **NO** via the public domain | The Cloudflare Tunnel's ingress list has exactly two hostname rules (both → Traefik on port 80) plus a catch-all 404 — SSH is a separate, direct LAN connection to the host's own port 22, never routed through Cloudflare at all |
| Engine-worker / payout-worker | **NO** | No `ports:` mapping, no Traefik label — internal-only, `/metrics` scraped by Prometheus on the internal network only |

## Admin console: the one open decision

The admin console is fully built and functional (verified: unauthenticated
and garbage-bearer-token requests to `/dashboard` and the superadmin-only
`/audit-log` both correctly return `403` when tested directly against the
container) but has no public URL. This session's explicit decision (asked
and answered) was: **verify it's built and working, don't expose it
publicly** — its current access model (reach `127.0.0.1:8001` via SSH,
e.g. an SSH tunnel) is unchanged.

If that decision is ever revisited, exposing it safely needs two things
done first, in this order:
1. Fix `services/admin/app.py::_client_ip()` to read `X-Forwarded-For`
   (trusting it, since only Traefik itself — not the public internet —
   can reach the admin container's port) before trusting the raw
   connection IP, so `ADMIN_IP_ALLOWLIST` (if ever configured) doesn't
   silently break by seeing only Traefik's own address for every request.
2. Decide the routing shape: a real `admin.arada.fun` subdomain (clean,
   no path-collision risk, but needs a new cloudflared ingress entry +
   DNS record — a systemd-level config change, not just a container
   label) vs. enumerating admin's ~15 bare API path prefixes under the
   existing `arada.fun` hostname (no new DNS, but a real maintenance
   footgun: a future admin route not added to that list silently 404s
   instead of reaching admin). The recommended choice, if this is ever
   revisited, is the real subdomain — it's what the system's own existing
   docs (`docs/TELEBIRR_PRODUCTION_CHECKLIST.md`) already describe.

## RBAC: enforced at the API, not just hidden in the UI

Verified directly (not assumed from reading the code alone): a request
with no `Authorization` header, and separately a request with a garbage
bearer token, both against `/dashboard` and against the superadmin-only
`/audit-log`, returned `403` in every case when sent straight to the
admin container. The full permission-to-role table (`payments:view`,
`payments:view_raw_evidence`, `payments:approve`, `payments:configure`,
`audit:view`) is unchanged from `docs/TELEBIRR_ROLES_AND_ACCESS.md` and
was not touched this session.

## Secrets

- `MACRODROID_INGEST_TOKEN`: now correctly set in the real `.env` on the
  server (was briefly sitting in the template file instead — see the
  domain doc's "what changed" section). Never printed, in this
  conversation or anywhere else, during the fix.
- `PHONE_ENCRYPTION_KEY`: production's real value is confirmed (by hash
  comparison only, values never printed) to be **different** from the
  one committed to git history in `.env.example` — production is not
  using the exposed value. The exposed value remains in git history; see
  the domain doc's "remaining gaps" for what that does and doesn't affect.
- `payment_provider_availability.telebirr_sms` remains `enabled = false`
  in production, confirmed by direct query — the whole feature is wired
  and reachable end-to-end but the player-facing redemption path stays
  off until an explicit decision to flip it on.
