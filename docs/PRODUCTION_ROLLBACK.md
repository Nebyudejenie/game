# Production Rollback

Consolidates rollback procedures already documented piecemeal across
this repo's per-feature docs into one place. Every lever named here is
real and already built — this document doesn't add new mechanism, it
indexes what exists.

## The two kinds of rollback this platform has

1. **Feature-flag rollback** — flip a config value, no deploy, no
   downtime, seconds to take effect. Always try this first if the
   incident is scoped to one feature.
2. **Code rollback** — redeploy an older git SHA + (if needed) a
   migration downgrade. Slower, needed only when the flag-level lever
   doesn't cover the actual problem (a bug in code every rail shares,
   not one feature-specific flag).

## Feature-flag rollbacks (fastest, try these first)

| Feature | Lever | Effect |
|---|---|---|
| Any payment rail (Chapa/manual/Telebirr) | `payment_provider_availability` toggle, admin console → Provider Availability, superadmin | Stops new attempts through that rail instantly. Telebirr ingestion keeps accumulating evidence safely even while redemption is off (per `docs/TELEBIRR_SMS_OPERATIONS_GUIDE.md`). |
| A specific bonus rule | Deactivate the rule (Bonuses & Referrals screen) | Stops new grants under that rule; existing grants are unaffected (they already posted real ledger entries). |
| A specific admin account | Deactivate (Admin Users screen) | Revokes the account's current session immediately, not just future logins. |
| The whole Notification Center | No global kill switch by design (each campaign's own cancel is itself an audited action with a specific actor) | Cancel individual `scheduled`/`queued` campaigns; to stop *all* bot-originated messages including transactional ones, only a `bot` container rollback (below) reaches that far. |
| Admin IP allowlist | `ADMIN_IP_ALLOWLIST` env var | Empty = unrestricted (dev-friendly default); set to lock admin access to known networks if a credential compromise is suspected. |

## Code rollback (application)

```bash
# On the production host, inside the deploy checkout:
git log --oneline -10          # confirm current HEAD and the target SHA
git checkout <previous-good-sha>
docker compose -f deploy/docker-compose.prod.yml up -d --force-recreate --no-deps <affected-service>
```

Only recreate the specific service(s) the rollback actually needs to
touch — this codebase's own convention (see `payout_worker.py`'s own
docstring reasoning for bundling several sweeps into one process)
already means several unrelated jobs share one container in a few
places; recreating `payments` also restarts the bonus-wagering sweep and
4 other sweeps that share that process, for example — expected, not a
bug, but worth knowing before assuming a narrow blast radius.

## Code rollback (database migration)

**Never `alembic downgrade` against production data casually** — a
downgrade that drops a column or table is only safe if nothing written
since the upgrade needs to survive it. Before any migration downgrade in
production:

1. Take a real backup first (`deploy/backup.sh`) — see `docs/
   DISASTER_RECOVERY.md` if a scheduled one may not already exist.
2. Confirm the specific migration's own `downgrade()` function doesn't
   silently drop real data the newer code path has already written (read
   it — every migration in `migrations/versions/` is a plain, readable
   Python file).
3. `alembic -c migrations/alembic.ini downgrade -1` (or to a specific
   revision) only after the above.

This session's own newest migrations are all additive (new tables only:
`notification_*`, `bot_i18n_overrides`, `bonus_rules`/`bonuses`) — their
downgrades are plain `DROP TABLE`s with no data-loss risk to anything
*other* than that feature's own rows, since nothing else in the schema
references them.

## Per-feature rollback notes (cross-referenced, not duplicated)

- **Telebirr**: `docs/TELEBIRR_PRODUCTION_CHECKLIST.md` section K —
  disable via Provider Availability; token rotation procedure documented
  there for a suspected device/token compromise specifically.
- **Notification Center crash recovery**: no rollback needed for the
  reclaim-sweep fix itself (it's a pure addition — a delivery either
  reclaims correctly or the worst case reverts to this feature's
  pre-existing behavior of a stuck row, not a new failure mode).
- **Bonus wagering sweep**: disabling is the same lever as any other
  `payout_worker`-hosted sweep — recreating that container without the
  code that starts the sweep (an older SHA) stops new conversions; grants
  already converted to cash are, correctly, not reversible via a rollback
  (they're real, already-settled ledger transactions).

## After any rollback

1. Re-run the relevant smoke test from `docs/PRODUCTION_READINESS.md`'s
   own acceptance matrix for whatever was rolled back.
2. Record the rollback itself in a way that survives — this repo's own
   audit log (`admin_audit_log`) captures *admin-console* actions
   automatically; a code/infra-level rollback (git checkout, container
   recreate) is not itself audited by this application and needs its own
   real incident record (see `docs/INCIDENT_RESPONSE.md`).
3. Don't consider the incident closed until the root cause — not just
   the symptom — has a real fix, even if the rollback itself bought time.
