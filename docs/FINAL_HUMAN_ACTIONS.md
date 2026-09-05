# Final Human Actions

Everything in this document requires a human, external access, a
physical device, real money, or an authority outside engineering.
Nothing here is AI-fixable — every item that *was* AI-fixable has
already been fixed and is recorded in `docs/LAUNCH_BLOCKERS.md`
(Category A) and `docs/RELEASE_CANDIDATE.md`, not repeated here. This
document is intentionally narrower than `docs/LAUNCH_BLOCKERS.md` — same
underlying items, re-projected into one fixed action schema for an
operator working through them one at a time.

---

### 1. Enable HTTPS redirect at the Cloudflare zone

- **ACTION**: Enable "Always Use HTTPS" (or an equivalent redirect Page
  Rule) for the `arada.fun` zone in the Cloudflare dashboard.
- **WHY**: Confirmed, current gap — plain HTTP is currently served
  without a redirect, exposing session tokens and payment flows to
  network-level interception.
- **EXACT COMMAND / UI STEPS**: Cloudflare dashboard → the zone → SSL/TLS
  → Edge Certificates → toggle "Always Use HTTPS" on.
- **EXPECTED RESULT**: `curl -sI http://arada.fun/` returns a
  `301`/`308` redirect to `https://arada.fun/`.
- **EVIDENCE TO CAPTURE**: The `curl` output showing the redirect.
- **PASS CRITERIA**: Redirect confirmed for all 5 hostnames.
- **FAIL CRITERIA**: Any hostname still returns `200` over plain HTTP.
- **ROLLBACK**: Disable the setting — reversible with no data impact.

### 2. Install and enable the backup systemd timers

- **ACTION**: Copy `deploy/systemd/jobingo-*.{service,timer}` to
  `/etc/systemd/system/` on the production host and enable them.
- **WHY**: The backup/basebackup/WAL-pruning scripts are real and tested,
  but nothing has ever scheduled them — confirmed via grep, no cron or
  timer references them anywhere.
- **EXACT COMMAND / UI STEPS**: See `deploy/systemd/README.md` — confirm
  the `WorkingDirectory=` placeholder matches the real deploy path first,
  then `sudo systemctl daemon-reload && sudo systemctl enable --now
  jobingo-backup.timer jobingo-basebackup.timer jobingo-prune-wal.timer`.
- **EXPECTED RESULT**: `systemctl list-timers | grep jobingo` shows 3
  timers with real `NEXT` values; a manually-triggered run produces a
  real dump file.
- **EVIDENCE TO CAPTURE**: `systemctl list-timers` output; `ls -la
  backups/` showing a fresh file.
- **PASS CRITERIA**: All 3 timers active; a manual run succeeds and
  produces output.
- **FAIL CRITERIA**: Any timer inactive, or a manual run exits non-zero.
- **ROLLBACK**: `systemctl disable --now <unit>` — reversible, doesn't
  touch existing backups.

### 3. Run a real restore drill

- **ACTION**: Restore the most recent real production backup onto a
  disposable instance (never production itself) and verify the data.
- **WHY**: The restore mechanism is tested against synthetic data only;
  never proven against a real production backup file.
- **EXACT COMMAND / UI STEPS**: `./deploy/restore.sh <real-backup-file>
  jobingo_restore_drill` against a throwaway Postgres instance, then
  `SELECT count(*) FROM users;` (and a few other real tables) to sanity-
  check row counts against production's known scale.
- **EXPECTED RESULT**: Restore completes without error; row counts are
  plausible.
- **EVIDENCE TO CAPTURE**: Restore duration, exit code, row-count query
  output.
- **PASS CRITERIA**: Clean restore, plausible data.
- **FAIL CRITERIA**: Restore errors, or data looks truncated/corrupted.
- **ROLLBACK**: N/A — this never touches production data (restores to a
  disposable target only).

### 4. Confirm production is running current code

- **ACTION**: SSH to the production host and compare its checked-out
  commit to this repo's `HEAD`.
- **WHY**: No network path exists from any AI session to
  `cosmic@192.168.1.173` — this has never been confirmed.
- **EXACT COMMAND / UI STEPS**: `ssh cosmic@192.168.1.173 'cd
  /home/cosmic/game && git log -1 --oneline'`, compare to `git rev-parse
  HEAD` run locally.
- **EXPECTED RESULT**: Matching (or newer) commit; clean `git status`.
- **EVIDENCE TO CAPTURE**: Both commit hashes, side by side.
- **PASS CRITERIA**: Production is at this release candidate's commit or
  later, with a clean working tree.
- **FAIL CRITERIA**: Production is behind, or has uncommitted local
  changes.
- **ROLLBACK**: N/A — a read-only check.

### 5. Confirm alerting actually pages a real person

- **ACTION**: Trigger one real, controlled test alert end to end.
- **WHY**: 9 real alert rules exist; whether Alertmanager has a working
  receiver has never been confirmed.
- **EXACT COMMAND / UI STEPS**: `amtool alert add
  alertname=LaunchReadinessDrill severity=critical
  --alertmanager.url=<url>`, confirm a real page arrives, then `amtool
  silence add alertname=LaunchReadinessDrill --alertmanager.url=<url>`
  to resolve it.
- **EXPECTED RESULT**: A real person receives a real page; a resolution
  notification follows.
- **EVIDENCE TO CAPTURE**: Screenshot/log of the received page and its
  resolution.
- **PASS CRITERIA**: Page received and resolved.
- **FAIL CRITERIA**: No page received.
- **ROLLBACK**: The silence command above clears the drill alert.

### 6. Re-verify all 5 production hostnames externally

- **ACTION**: Run the 5-hostname HTTPS/Cloudflare check from a machine
  with real internet access.
- **WHY**: Last confirmed 2026-09-05, several commits stale.
- **EXACT COMMAND / UI STEPS**: `curl -sI https://<host>/healthz` for
  `arada.fun`, `payments.arada.fun`, `admin.arada.fun`,
  `finance.arada.fun`, `agent.arada.fun` — see
  `docs/PRODUCTION_HOST_VERIFICATION.md` section 6.
- **EXPECTED RESULT**: `200` with a `cloudflare` server header, each.
- **EVIDENCE TO CAPTURE**: All 5 `curl` outputs.
- **PASS CRITERIA**: All 5 pass.
- **FAIL CRITERIA**: Any non-200 or missing Cloudflare header.
- **ROLLBACK**: N/A — read-only.

### 7. Real-world Telegram/Mini App smoke test

- **ACTION**: Play a full round on a real phone, in real Telegram.
- **WHY**: Real WebView/network behavior cannot be verified from any
  sandbox.
- **EXACT COMMAND / UI STEPS**: Open the real bot, join a room, play a
  round to a claim, background/foreground mid-round, force a network drop
  and confirm WS reconnect.
- **EXPECTED RESULT**: Full round completes; reconnect recovers state
  correctly.
- **EVIDENCE TO CAPTURE**: Screen recording.
- **PASS CRITERIA**: No crash, no stuck state, correct balance after.
- **FAIL CRITERIA**: Any of the above fails.
- **ROLLBACK**: N/A — a real-device test, no system change.

### 8. Controlled real-money payment test (Chapa)

- **ACTION**: One small real deposit, one small real withdrawal.
- **WHY**: Confirms real credentials/webhook delivery/settlement timing —
  a sandbox pass doesn't guarantee this.
- **EXACT COMMAND / UI STEPS**: Perform both through the real Mini App
  with real money at the minimum amount the business authorizes, then
  `SELECT * FROM ledger_transactions ORDER BY id DESC LIMIT 5;`.
- **EXPECTED RESULT**: Correct ledger entries, correct resulting balance.
- **EVIDENCE TO CAPTURE**: Transaction reference, matching ledger rows.
- **PASS CRITERIA**: Ledger matches the real money moved, exactly.
- **FAIL CRITERIA**: Any mismatch, delay beyond expectation, or missing
  entry.
- **ROLLBACK**: A real discrepancy needs a real, audited corrective
  ledger entry (`docs/INCIDENT_RESPONSE.md`'s ledger-imbalance
  playbook) — never a raw balance edit.

### 9. Telebirr SMS controlled real-world test

- **ACTION**: Complete the full physical-device + real-transaction
  checklist before ever enabling `telebirr_sms`.
- **WHY**: The one payment rail whose correctness depends entirely on a
  real phone forwarding real carrier SMS content.
- **EXACT COMMAND / UI STEPS**: `docs/TELEBIRR_PRODUCTION_CHECKLIST.md` +
  `docs/TELEBIRR_MACRODROID_QUICK_SETUP.md`, end to end, with a real
  device and a real small transaction.
- **EXPECTED RESULT**: Real SMS ingested, evidence redeemed, correct
  ledger credit.
- **EVIDENCE TO CAPTURE**: Completed checklist, resulting ledger row.
- **PASS CRITERIA**: Checklist fully passes.
- **FAIL CRITERIA**: Any step fails.
- **ROLLBACK**: Leave `payment_provider_availability.telebirr_sms =
  false` (the current, correct default) until this passes.

### 10. Legal / regulatory review

- **ACTION**: Obtain real legal counsel review for every jurisdiction
  this product will operate in.
- **WHY**: Real-money gambling regulation is jurisdiction-specific; no
  engineering work substitutes for this.
- **EXACT COMMAND / UI STEPS**: Engage qualified local counsel; use
  `docs/RESPONSIBLE_GAMING_REQUIREMENTS.md` as the technical-control
  reference point for their review.
- **EXPECTED RESULT**: A written legal opinion per jurisdiction.
- **EVIDENCE TO CAPTURE**: The written opinion(s), dated.
- **PASS CRITERIA**: Written confirmation the product is compliant, or a
  clear list of what must change to become compliant.
- **FAIL CRITERIA**: No review obtained before public launch.
- **ROLLBACK**: N/A.

### 11. Telegram platform policy review

- **ACTION**: Review this product's real-money mechanics against
  Telegram's current live Bot API / Mini App / payments policy.
- **WHY**: A platform-policy violation can get the bot banned regardless
  of code correctness. One load-bearing question:
  is real-money gambling via external (non-Telegram) payment rails
  permitted at all — every other policy question is downstream of it.
- **EXACT COMMAND / UI STEPS**: Read Telegram's current policy documents;
  use `docs/PLATFORM_POLICY_REVIEW.md` as the capability/decision
  reference point.
- **EXPECTED RESULT**: A written determination.
- **EVIDENCE TO CAPTURE**: The determination, dated, citing the specific
  policy sections reviewed.
- **PASS CRITERIA**: Written confirmation of permission, or a clear
  redesign requirement.
- **FAIL CRITERIA**: No review obtained before public launch.
- **ROLLBACK**: N/A.

### 12. Decide: accept the historical secret exposure, or rewrite git history

- **ACTION**: A business decision on `PHONE_ENCRYPTION_KEY`'s historical
  exposure (5 commits, confirmed to differ from the real production key).
- **WHY**: Rewriting history (BFG/`git filter-repo`) breaks every existing
  clone/fork/open PR — disruptive enough that it shouldn't happen without
  an explicit decision.
- **EXACT COMMAND / UI STEPS**: Decide accept-risk vs. schedule-rewrite;
  if rewriting, coordinate with everyone holding a clone first.
- **EXPECTED RESULT**: A recorded decision.
- **EVIDENCE TO CAPTURE**: The decision itself, dated.
- **PASS CRITERIA**: A decision is made and recorded, either way.
- **FAIL CRITERIA**: Left silently undecided indefinitely.
- **ROLLBACK**: N/A — this action *is* the decision point.

### 13. Decide: build a fuller Bonus Rules lifecycle, or keep the current one

- **ACTION**: Decide whether `bonus_rules`' current `is_active` +
  `starts_at`/`ends_at` + RBAC + audit trail is sufficient, or whether a
  named draft/review/approved/scheduled/paused/expired state machine is
  worth building on top of it.
- **WHY**: The current design already prevents an unreviewed rule from
  going live accidentally; a fuller lifecycle is real, additional
  process, not a fix for a known gap.
- **EXACT COMMAND / UI STEPS**: Product decision, informed by
  `docs/LAUNCH_BLOCKERS.md` LB-D0.
- **EXPECTED RESULT**: A recorded decision.
- **PASS/FAIL CRITERIA**: Decided vs. left ambiguous.
- **ROLLBACK**: N/A.

### 14. Decide: build "Promotions" as one unified entity

- **ACTION**: Decide whether Bonus Rules + the Notification Center's
  "Announce this rule" link should become one named "Promotion" object
  with its own lifecycle.
- **WHY**: Today's two-system-plus-a-link design already covers the
  functional requirement; unifying them is a real, separate scope
  decision (`docs/LAUNCH_BLOCKERS.md` LB-D4), not a bug fix.
- **PASS/FAIL CRITERIA**: Decided vs. left ambiguous.
- **ROLLBACK**: N/A.

### 15. Load testing against production-like infrastructure

- **ACTION**: Run a real load test (concurrent rooms/players, concurrent
  deposits) against a staging environment sized like production.
- **WHY**: This sandbox's dev stack isn't representative of real load.
- **EXPECTED RESULT**: Latency/error-rate figures at a defined concurrent-
  user target.
- **EVIDENCE TO CAPTURE**: The load-test report.
- **PASS CRITERIA**: Meets whatever latency/error-rate target the
  business sets.
- **FAIL CRITERIA**: Does not meet that target.
- **ROLLBACK**: N/A — a staging-only exercise.

---

## What is deliberately NOT in this document

Every AI-fixable item from this pass — the ledger reconciliation
scheduling, the Bingo acceptance-audit gap-fills, the zero-player-room
verification, the malformed-claim handling, the Notification Center
Redis-outage test, the `run_active_rooms()` batching cap, the prepared
(but not yet installed) systemd units — is already done and recorded in
`docs/LAUNCH_BLOCKERS.md` (Category A) and `docs/RELEASE_CANDIDATE.md`.
Nothing AI-fixable is mixed into this list.
