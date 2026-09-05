# Platform Policy Review

Separates three questions that are easy to conflate when a product is
built quickly: **can the code do this** (a technical capability),
**does Telegram's own policy allow this** (a platform rule this project
doesn't control and hasn't reviewed), and **has anyone actually decided
we should do this** (a business/product call). A "yes" in the first
column is never evidence for the second or third.

| Technical capability | Platform policy (Telegram) | Legal requirement | Business decision |
|---|---|---|---|
| The bot/Mini App can move real money (deposits, withdrawals, in-game stakes) via a Telegram bot and Mini App. | UNKNOWN — not reviewed. Telegram's Bot API and Mini App terms have historically drawn a distinction between "games of skill" content and real-money gambling; whether this product's specific mechanics and payment flow are compliant with Telegram's *current* live policy has not been checked against the actual policy text (`docs/LAUNCH_BLOCKERS.md` LB-D1). | UNKNOWN — jurisdiction-specific gambling regulation (LB-D2). | Decided and built — this is the core product. |
| The Mini App can display a live wallet balance and let a user initiate a deposit/withdrawal from inside Telegram's WebView. | UNKNOWN — not reviewed against Telegram's current Mini App payments guidance specifically (separate from Telegram's own in-house Telegram Stars/payments system, which this product does not use — it integrates external Ethiopian payment rails instead). | UNKNOWN | Decided and built. |
| The bot can send unsolicited promotional messages (bonus/referral announcements) to users who have interacted with it. | UNKNOWN — Telegram's Bot API has its own rules about unsolicited/spam messaging distinct from gambling-specific rules; not reviewed. | UNKNOWN (marketing-consent law varies by jurisdiction) | Built with real technical safeguards regardless of the policy answer: self-excluded/banned/cooling-off users are unconditionally excluded from every send (`packages/core/campaigns.py::_build_where()`), and every campaign is a real, RBAC-gated, audited admin action — not a design that assumes the policy question away, but doesn't answer it either. |
| The referral program pays a real cash/bonus reward for inviting new users who deposit. | UNKNOWN — Telegram doesn't generally prohibit referral programs, but a *real-money* referral incentive specifically tied to gambling deposits is exactly the kind of feature a platform-policy or legal review should check explicitly, not assume is fine because generic referral programs are common. | UNKNOWN | Decided and built this session (the Referral & Bonus Management feature), with real fraud guards (shared-payout-account detection, one-reward-per-referee DB constraint) — those guards address *fraud*, not the separate policy/legal question. |
| The bot collects and stores a user's phone number (encrypted) for identity/payment purposes. | Likely fine under Telegram's general data-handling norms for bots that need it for their function, but not formally reviewed. | UNKNOWN (data protection law — is there a local equivalent of GDPR-style consent/retention requirements?) | Decided and built (`PHONE_ENCRYPTION_KEY`-encrypted storage, `phone_lookup_hash` for lookup without decryption) — a real technical safeguard for the *security* of this data; doesn't answer whether *collecting* it at all, or for how long, meets a legal retention/consent requirement. |
| The platform can operate without Telegram's official in-app payments (Telegram Stars) by linking out to/embedding external Ethiopian payment rails (Chapa, Telebirr). | UNKNOWN — this is exactly the kind of Mini App payments-policy question Telegram has tightened over time in various product categories; not reviewed against current policy. | N/A (this row is a platform-policy question, not primarily a legal one) | Decided and built — the entire payments architecture assumes this is permitted; **if a platform-policy review finds otherwise, this is a significant re-architecture, not a config flag**, so this is the single highest-leverage item to resolve early via LB-D1. |

## Why this table stays mostly UNKNOWN

Every UNKNOWN above requires reading Telegram's actual, current live Bot
API / Mini App / payments policy documents and judging this specific
product's real mechanics against them — a policy interpretation task,
not a code-reading task. This session has no authority to certify
platform-policy compliance and has not attempted to (see
`docs/LAUNCH_BLOCKERS.md` LB-D1). Filling in "probably fine" here would
be worse than leaving it UNKNOWN — it would look like a review happened
when it didn't.

## The one item worth escalating first

Of everything in this table, **whether a real-money gambling product
built as a Telegram Bot + Mini App, using external (non-Telegram)
payment rails, is permitted under Telegram's current platform policy at
all** is the load-bearing question — every other row is downstream of
it. If the answer is "no" or "only with restrictions," that reshapes the
product, not just a feature. Recommend resolving this one first, before
investing further engineering effort in policy-adjacent features.
