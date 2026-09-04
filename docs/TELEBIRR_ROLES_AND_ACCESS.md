# Telebirr Roles and Access

Populated directly from `services/admin/rbac.py`'s `PERMISSIONS` dict and
`services/bot/handlers.py`'s agent authorization filter — verified against
the live code, not assumed. See `docs/TELEBIRR_SMS_OPERATIONS_GUIDE.md`
for the full narrative; this file is the quick-reference matrix.

## The five real actors in this system

| Actor | What they are | Where their identity/authorization lives |
|---|---|---|
| **Player** | Any authenticated Telegram Mini App user | `users` table; authenticated via Telegram `initData` (no separate role — every registered player has the same access) |
| **Payment Agent** | A person authorized to forward SMS via the private Telegram bot | `payment_agents` table (`telegram_user_id`, `is_active`) |
| **Support** (admin role) | Admin console role | `admin_users.role = 'support'` |
| **Ops** (admin role) | Admin console role | `admin_users.role = 'ops'` |
| **Finance** (admin role) | Admin console role | `admin_users.role = 'finance'` |
| **Superadmin** (admin role) | Admin console role, full access | `admin_users.role = 'superadmin'` |
| **MacroDroid device** | Not a role — a bearer-token-authenticated HTTP caller | `MACRODROID_INGEST_TOKEN` (single shared secret today, §4/§24 of the ops guide) |

## Full capability matrix

| Capability | Player | Payment Agent | Support | Ops | Finance | Superadmin |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| Submit wallet redemption (`POST /api/wallet/deposits/telebirr/redeem`) | **YES** | NO | NO | NO | NO | NO |
| Ingest SMS via Telegram bot | NO | **YES** (only if `is_active=true`) | NO | NO | NO | NO |
| Ingest SMS via MacroDroid | NO (device-authenticated, not role-based) | — | — | — | — | — |
| List/search payment evidence (`GET /telebirr-evidence`) | NO | NO | **YES** | **YES** | **YES** | **YES** |
| View raw SMS text (`GET /telebirr-evidence/{id}/raw-sms`) | NO | NO | NO | NO | **YES** | **YES** |
| Resolve evidence status (`POST /telebirr-evidence/{id}/resolve`) | NO | NO | NO | NO | **YES** | **YES** |
| List payment agents (`GET /payment-agents`) | NO | NO | **YES** | **YES** | **YES** | **YES** |
| Create/deactivate payment agents (`POST`/`PATCH /payment-agents`) | NO | NO | NO | NO | NO | **YES** |
| List manual/Telebirr destinations (`GET /manual-payment-destinations`) | NO | NO | **YES** | **YES** | **YES** | **YES** |
| Create/edit the recognized recipient (`POST`/`PATCH /manual-payment-destinations`) | NO | NO | NO | NO | NO | **YES** |
| View provider availability (`GET /payment-provider-availability`) | NO | NO | **YES** | **YES** | **YES** | **YES** |
| Enable/disable `telebirr_sms` (`PATCH /payment-provider-availability/...`) | NO | NO | NO | NO | NO | **YES** |
| View admin audit log (`GET /audit-log`) | NO | NO | NO | NO | NO | **YES** (`audit:view` is superadmin-only) |

Every "NO" for an admin role above returns a real `403 Forbidden` from the
server (`Depends(require("<permission>"))` in `services/admin/app.py`) —
enforcement is server-side on every request, never a frontend-only
hide/show. The admin console frontend additionally hides buttons/screens
a role can't use, purely as a UX convenience — verified the backend is
the real gate, not the UI.

## Exact permission → role mapping (verbatim from `rbac.py`)

```python
"payments:view":               {"support", "finance", "ops", "superadmin"}
"payments:view_raw_evidence":  {"finance", "superadmin"}
"payments:approve":            {"finance", "superadmin"}
"payments:configure":          {"superadmin"}
"audit:view":                  {"superadmin"}
```

## Why the boundaries are drawn where they are

- **`payments:view` is broad (all four roles)**: seeing *that* a payment
  exists, its amount, and its status is routine payment-support work
  (support/ops need this to answer "did my payment go through" questions).
- **`payments:view_raw_evidence` is narrower (finance/superadmin only)**:
  the raw SMS text contains a payer's masked phone number and full name —
  least-privilege access to that specific detail, separate from knowing a
  payment happened at all.
- **`payments:approve` matches `payments:view_raw_evidence`**: resolving a
  disputed/rejected/blocked row is a judgment call that typically requires
  having actually read the evidence first, so the same two roles hold
  both.
- **`payments:configure` is superadmin-only**: this is the single
  highest-leverage lever in the whole feature — whoever can edit the
  recognized recipient controls where every future "transferred to"
  payment's money is considered legitimate, and whoever can add a payment
  agent controls who can inject payment evidence at all. A compromised
  `finance` account can approve or reject individual payments (bounded
  blast radius); a compromised account with `payments:configure` could
  redirect the whole rail. This mirrors the exact same reasoning already
  applied to the pre-existing manual-deposit-destination configuration.
- **Payment Agent is not an admin role at all**: an agent's only
  capability is *submitting evidence for review* — they cannot see other
  players' data, cannot approve their own submission, cannot configure
  anything. The system already treats their submission with exactly the
  same skepticism as an anonymous MacroDroid device: parsed, then
  recipient-validated, before it becomes usable by anyone.
- **Players have zero payment-administration access of any kind** — their
  only interaction with this feature is the one reference-only redemption
  endpoint.
