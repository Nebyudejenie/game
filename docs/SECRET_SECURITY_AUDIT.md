# Secret Security Audit

Report-only, as required: no secret value appears anywhere in this
document, only its type, where it lives, whether it has ever been
exposed, whether it's been rotated, and the residual risk. Built by
directly inspecting `.env.example`'s full git history (`git log --all -p
-- .env.example`, output piped through a redaction filter before this
document was written — no raw value was ever transcribed here) and this
repo's own `.gitleaksignore`.

| SECRET_TYPE | LOCATION | EXPOSED? | ROTATED? | REMAINING_RISK |
|---|---|---|---|---|
| Database connection string (`DATABASE_URL`, `DATABASE_URL_SYNC`) | `.env.example` (placeholder), real value only in the actual runtime environment | No — every historical value in `.env.example` for these keys is a trivial `localhost`-only dev default, never a real credential | N/A | None — never a real secret in this file. |
| Redis connection string (`REDIS_URL`) | Same as above | No — same, trivial `localhost` dev default throughout history | N/A | None. |
| Phone-number field encryption key (`PHONE_ENCRYPTION_KEY`) | `.env.example` | **Yes** — a real, non-placeholder value was committed and present across 5 commits (2026-08-24 to 2026-09-05, 12 days), removed from `HEAD` before this pass began | Partially — the identical value was also hardcoded as a test-fixture default in `tests/integration/conftest.py`; that copy was rotated to a freshly generated value this session that has never appeared in any commit. The *original* exposed value's status as production's real key: confirmed, via an earlier session's own hash comparison, to differ from it. | Low, not zero — the exposed value remains permanently recoverable from git history by anyone with an existing clone. It is confirmed not to be the live production key, but the value itself is still "out" and should not be reused anywhere going forward. Decision pending on whether a full history rewrite is worth the disruption (`docs/LAUNCH_BLOCKERS.md` LB-D3) — a business call, not a technical one. |
| Telegram bot token (`TELEGRAM_BOT_TOKEN`) | `.env.example` (placeholder only) | No — never a non-placeholder value in any commit found | N/A | None found; standard operational hygiene (rotate via BotFather if ever suspected compromised) applies regardless. |
| Telegram webhook secret (`TELEGRAM_WEBHOOK_SECRET`) | `.env.example` (placeholder only) | No | N/A | None found. |
| Chapa payment API key (`CHAPA_API_KEY`) | `.env.example` (placeholder only) | No | N/A | None found. |
| SantimPay / ArifPay API keys | `.env.example` (placeholder only) | No — these providers were never actually integrated with live credentials (see `DECISIONS.md`'s own entry on why: their API docs were network-unreachable from every session's environment) | N/A | None — no real credential for either has ever existed in this project. |
| MacroDroid ingestion token (`MACRODROID_INGEST_TOKEN`) | `.env.example` (placeholder only) | No | N/A | None found. |
| Admin/finance/agent account passwords and TOTP secrets | Never committed anywhere — generated at account-creation time, hashed (bcrypt) before storage, real TOTP secrets shown once at creation/reset and never logged (confirmed: `packages/core/logging.py`'s redaction list, and no test or fixture file was found embedding a real production admin credential) | No | N/A | None found. This is architecturally different from the `.env.example` secrets above — these never exist as a static file value to begin with. |
| TLS/cloudflared tunnel credentials | Explicitly gitignored (`deploy/.env`, cloudflared's own credential files) — confirmed via `.gitignore` and this pass's own review; never found committed | No | N/A | None found. |
| Private key files (`.pem`/`.key`/`.pfx`/`id_rsa*`) | N/A — none tracked anywhere in this repository at any commit | No | N/A | None. `.gitignore` gap for these patterns was closed earlier this engagement (defense-in-depth against a future accidental `git add .`, not a response to an actual leak). |

## Methodology note

This audit's EXPOSED? column reflects **git history**, not just current
`HEAD` — a value removed from the latest commit is not the same claim as
"never existed." Every row above was checked against the full history of
`.env.example` (the one file where a real secret would most plausibly
have been pasted by mistake), cross-referenced against this repo's own
`.gitleaksignore` (which documents exactly one confirmed-benign
allowlisted hit, matching the `PHONE_ENCRYPTION_KEY` row above).

## What this audit did not check

- Any secret that might exist only in the real production environment's
  actual `.env` file (never committed, by design, so not inspectable
  from a git-history audit) — its rotation history and exposure risk are
  unknowable from this repository alone.
- Third-party provider-side credential rotation history (e.g., whether
  Chapa's own dashboard shows any suspicious API key usage) — outside
  what a code/git audit can determine.
