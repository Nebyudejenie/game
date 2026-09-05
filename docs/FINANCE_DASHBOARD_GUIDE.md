# Finance Dashboard Guide

The finance dashboard is a login to the one admin console
(`services/admin` + `web/admin` — see `docs/ADMIN_DASHBOARD_GUIDE.md`),
not a separate application. A `finance`-role `admin_users` account,
logging in at `https://finance.arada.fun` (or `admin.arada.fun` — same
console, same login, either hostname works), sees exactly the screens
finance actually needs; the console is identical code and identical
data either way.

## Why finance is a role, not its own app

Building a second frontend and a second set of API routes just to show
the same underlying data with a different URL would mean maintaining two
copies of every payments/ledger query — precisely the kind of duplication
"never build a second wallet, a second ledger" already rules out for the
payment feature itself. Applying that same discipline here: finance gets
its own hostname to log in at, real RBAC-enforced screens, and zero
duplicate code.

## What a finance login actually sees

Everything below is a real, live read from the same tables the Bingo
engine and ledger already treat as canonical — never a separate
finance-side balance computation.

| Screen | What it shows | Backing query |
|---|---|---|
| Dashboard | Active rounds/rooms, stakes today, payouts today, house revenue today, pending withdrawals | `GET /dashboard` |
| Payments | All payments (Chapa, manual, Telebirr) with status/amount/date | `GET /payments` |
| Manual Deposits / Withdrawals | Pending and historical manual-rail requests, approve/reject | `services/admin/queries.py` |
| Payment Destinations | Configured recipient accounts (name/phone/account, effective dates) | `GET/POST/PATCH /manual-payment-destinations` — editing is `payments:configure`, superadmin-only even for a finance login |
| **Telebirr Evidence** | Every ingested SMS's parsed outcome: reference, amount, status, source (MacroDroid device or which agent), timestamp. **Raw SMS text** (`GET /telebirr-evidence/{id}/raw-sms`) and **resolve** (`POST /telebirr-evidence/{id}/resolve`) are finance-and-superadmin-only — this is the one place raw payer/recipient text is visible, and it's gated exactly there | `GET /telebirr-evidence` |
| Payment Agents | The agent allowlist (view only — adding/deactivating is superadmin-only) | `GET /payment-agents` |
| Provider Availability | Which rails are live (`chapa`/`manual`/`telebirr_sms`, `in`/`out`) — view only, toggling is superadmin-only | `GET /payment-provider-availability` |
| Reports | Aggregate financial reporting | **finance + superadmin only** — hidden from support/ops in the nav, and a direct API call from those roles gets a real `403`, not just a hidden button |
| Rounds / Rooms | Round history, room configuration | shared with every role |

Reference/amount/date/status filtering already exists server-side in
`services/admin/queries.py` for every list screen above (payments,
telebirr evidence, manual deposits/withdrawals) — a finance user searching
a large dataset never downloads the whole table to the browser first.

## What finance cannot do

Everything money-*configuring* (editing the recognized recipient, adding
a payment agent, toggling a provider rail on/off, viewing the audit log)
stays `payments:configure`/`audit:view` — superadmin-only, enforced
server-side regardless of what the finance UI itself shows or hides. A
finance login attempting one of those API calls directly gets the exact
same `403` an unauthenticated request would.
