# Production Host Verification

One deterministic checklist an operator runs on (or against) the real
production host to confirm its actual state — not assumed from what this
repo says *should* be true. Every command below is real; none of them
have been run against production from this environment (no network path
exists to `cosmic@192.168.1.173`, a private LAN address — confirmed via
`git ls-remote prod` timing out). Run these for real, then record the
actual output somewhere durable (this file's "Last confirmed" line, an
incident ticket, wherever this team already keeps operational records).

Each row: the command, what a healthy result looks like, what a bad
result means, and the fix.

**Evidence to capture, for every single row below, no exceptions**: the
complete real stdout/stderr of the command as actually run, with a
timestamp — not a paraphrase, not "looked fine," not a memory of having
run it. Paste it into this file's own "Last confirmed" table at the
bottom (or an incident ticket, whichever this team already uses) next to
the row it verifies. A row with no captured evidence has not been
verified, regardless of what anyone recalls.

## 1. Host identity and reachability

| Command | Expected | Failure meaning | Remediation |
|---|---|---|---|
| `ssh cosmic@192.168.1.173 'hostname && uname -a'` | Returns a real hostname and kernel info | SSH itself fails | Confirm you're on the same LAN/VPN as the host; confirm the SSH key is authorized. |

## 2. Deployed code matches expectation

| Command | Expected | Failure meaning | Remediation |
|---|---|---|---|
| `ssh cosmic@192.168.1.173 'cd /home/cosmic/game && git log -1 --oneline'` | A commit hash matching (or later than) this repo's own `git rev-parse HEAD` | Production is running older code than expected | `git pull && docker compose -f deploy/docker-compose.prod.yml up -d --force-recreate` on the host, after confirming the newer code is actually meant to deploy. |
| `ssh cosmic@192.168.1.173 'cd /home/cosmic/game && git status --short'` | Empty | Uncommitted local changes exist on the production checkout | Investigate before doing anything else — this means someone edited code directly on the server, bypassing the normal deploy path. |

## 3. Containers are up and healthy

| Command | Expected | Failure meaning | Remediation |
|---|---|---|---|
| `ssh cosmic@192.168.1.173 'docker ps'` | All expected containers (`postgres`, `redis`, `gateway`, `admin`, `bot`, `payments`, `engine`, `cloudflared`, `prometheus`, `grafana`) show `Up`, none `Restarting`/`Exited` | A crash-looping or stopped container | `docker compose -f deploy/docker-compose.prod.yml logs --tail=200 <service>` to find the actual error, then `docs/INCIDENT_RESPONSE.md`'s matching playbook. |
| `ssh cosmic@192.168.1.173 'cd /home/cosmic/game && docker compose -f deploy/docker-compose.prod.yml ps'` | Same as above, compose's own view | Same as above | Same as above. |

## 4. Cloudflare Tunnel is actually running

| Command | Expected | Failure meaning | Remediation |
|---|---|---|---|
| `ssh cosmic@192.168.1.173 'systemctl status cloudflared'` (or the container-equivalent if run as one — confirm which on the real host) | `active (running)` | Tunnel process down — every public hostname becomes unreachable even if the app itself is fine | `systemctl restart cloudflared`; if it won't stay up, check its own logs for a config/credential error. |
| `ssh cosmic@192.168.1.173 'cloudflared tunnel info <tunnel-name>'` | Shows active connections | No connections shown | Same as above; also confirm the Cloudflare account/zone itself isn't showing an outage on Cloudflare's own status page. |

## 5. Network listeners match expectation

| Command | Expected | Failure meaning | Remediation |
|---|---|---|---|
| `ssh cosmic@192.168.1.173 'ss -lntup'` | Postgres (5432), Redis (6379), and each service's internal port bound only to expected interfaces (not `0.0.0.0` for anything that should be internal-only, e.g. Postgres/Redis should not be reachable from outside the docker network / host) | A database or cache port exposed to the public internet | This is a real security exposure if found — firewall it immediately, then investigate how the exposure happened (a compose file port mapping mistake is the most likely cause). |

## 6. External hostnames actually resolve and serve traffic

Run these from a machine with real internet access (not from the
production host itself — that only proves the origin works, not that the
public path through Cloudflare does):

| Command | Expected | Failure meaning | Remediation |
|---|---|---|---|
| `curl -sI https://arada.fun/healthz` | `HTTP/2 200`, `server: cloudflare` header present | Non-200, or no `cloudflare` header | If non-200: check the origin container directly (step 3). If no `cloudflare` header: DNS may not be routing through Cloudflare at all — check the zone's DNS records. |
| Same for `payments.arada.fun`, `admin.arada.fun`, `finance.arada.fun`, `agent.arada.fun` | Same | Same | Same |
| `curl -sI http://arada.fun/` | A `301`/`308` redirect to `https://arada.fun/` | A `200` served over plain HTTP | This is `docs/LAUNCH_BLOCKERS.md` LB-B1 — a confirmed, current gap. Fix: enable "Always Use HTTPS" in the Cloudflare dashboard for the zone. |

## 7. Backups are actually running

| Command | Expected | Failure meaning | Remediation |
|---|---|---|---|
| `ssh cosmic@192.168.1.173 'systemctl list-timers | grep jobingo'` | Three timers (`jobingo-backup`, `jobingo-basebackup`, `jobingo-prune-wal`) with real `NEXT`/`LAST` values | Nothing listed | Install `deploy/systemd/`'s prepared units — see that directory's own `README.md`. This is `docs/LAUNCH_BLOCKERS.md` LB-B2. |
| `ssh cosmic@192.168.1.173 'ls -la /home/cosmic/game/backups/ | tail -5'` | A dump file no older than 24-48h | Stale or empty | Same as above — the timers either aren't installed or are failing; check `journalctl -u jobingo-backup`. |

## 8. Alerting actually pages someone

| Command | Expected | Failure meaning | Remediation |
|---|---|---|---|
| A real, controlled test alert (`docs/LAUNCH_BLOCKERS.md` LB-B5's `amtool` procedure) | A real person receives a real page | No page received | Alertmanager's receiver config is missing or misconfigured — fix it, then re-test. A firing rule with no working receiver is equivalent to no alerting at all. |

---

## Last confirmed

*(Fill this in each time the checklist above is actually walked for
real, so the next person knows how stale this record is — the same
discipline `docs/DISASTER_RECOVERY.md`'s own checklist asks for.)*

| Section | Last confirmed | By | Result |
|---|---|---|---|
| 1-8 | Not yet run against real production from any session to date | — | — |

Sections 1-5 and 7-8 above require SSH access to `cosmic@192.168.1.173`,
which no session so far (including this one) has had. Section 6 was
performed directly against production in an earlier session
(2026-09-05, per `docs/PRODUCTION_DOMAIN_AND_CLOUDFLARE.md`) and passed
for all 5 hostnames at that time — several commits stale relative to
current `HEAD` now (see `docs/LAUNCH_BLOCKERS.md` LB-B6).
