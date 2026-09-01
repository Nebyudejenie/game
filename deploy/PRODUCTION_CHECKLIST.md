# Production launch checklist

Run this top to bottom, once, on the actual Proxmox VM this deploys to.
Every step here is a **real, one-time action** that can't be done from a
dev sandbox or committed to the repo — it needs either your Cloudflare
account, your GitHub repo settings, or real business account details.
See `README.md`'s own "CI/CD" and "Domain and Cloudflare Tunnel" sections
for the *why* behind each step; this file is just the *what*, in order.

**Scope note**: this checklist gets you live on **Chapa + Manual** (this
platform's own launch principle — see `DECISIONS.md`). Chapa itself is
optional: if you don't have live Chapa credentials yet, leave
`CHAPA_API_KEY`/`PAYMENTS_PUBLIC_BASE_URL` blank in step 4 and the app
honestly shows only the manual rail to players (verified: `services/
payments/availability.py`). The bot's own webhook (`PUBLIC_BASE_URL`,
step 4/5) is **not optional** either way — this codebase has no polling
fallback, so without it Telegram has no way to reach the bot at all.

- [ ] **0. Prerequisites on the Proxmox VM**
  - Docker + Docker Compose v2 installed (`docker compose version` works)
  - This repo checked out (`git clone` — the self-hosted runner setup in
    step 2 will manage its own checkout after this)
  - Ports 8000-8005 free (only used internally by the Docker network —
    nothing needs to be opened on a firewall/router; `cloudflared` in
    step 5 is the only thing that talks to the outside world)

- [ ] **1. Domain: point arada.fun's nameservers at Cloudflare**
  If not already done — in Hostinger, change arada.fun's nameservers to
  the two Cloudflare gave you when you added the site to your Cloudflare
  account. DNS propagation can take up to 24h, though it's usually
  faster. You can proceed with the rest of this checklist while waiting.

- [ ] **2. Register the self-hosted GitHub Actions runner**
  On GitHub: **Settings → Actions → Runners → New self-hosted runner**,
  Linux/x64. Run the setup script it gives you (`./config.sh --url ...
  --token ...`) directly on the Proxmox VM, then `./run.sh` (or install
  it as a systemd service via `./svc.sh install && ./svc.sh start` so it
  survives a reboot). No custom label needed — `cd.yml` targets
  `runs-on: self-hosted` generically.
  Verify: `gh api repos/Nebyudejenie/game/actions/runners` should list it.

- [ ] **3. (Recommended) Require manual approval before each deploy**
  **Settings → Environments → production → Required reviewers** → add
  yourself. `can_admins_bypass` stays available if you're ever locked
  out, and GitHub allows self-approval by default (no separate
  "prevent self-review" is on), so this adds a genuine pause-and-confirm
  before every production deploy without any risk of locking yourself
  out of your own pipeline — worth it before this handles real money.
  *(I generated the values below but couldn't apply this one setting
  myself — a repo-settings write was blocked by this session's own
  permission gate. Two-minute manual step if you want it.)*

- [ ] **4. Create `deploy/.env`** on the Proxmox VM (never commit this)
  ```bash
  cp deploy/.env.prod.example deploy/.env
  ```
  Fill in:
  ```
  POSTGRES_PASSWORD=8dGkZM5YrsgH6ZmKpaNzrWejmbQKB_TG4RyTkCR1d-w
  PHONE_ENCRYPTION_KEY=3faa4535f138ede8a5f5ed1a8ae1580da4a414209c443d51d993abed258fd2a1
  TELEGRAM_WEBHOOK_SECRET=WFC-B_n-PZEUYPf5j0v2yk8OQpctZqo_y0EQL08x75w
  ```
  (Three real, securely-generated values — safe to use as-is. Treat this
  file as a secret from the moment you paste these in: if it's ever
  exposed, rotate `POSTGRES_PASSWORD` for real and regenerate the other
  two the same way, `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.)

  Values only you have — get these from Telegram's @BotFather and (if
  using Chapa) your Chapa merchant dashboard:
  ```
  TELEGRAM_BOT_TOKEN=<from @BotFather>
  TELEGRAM_BOT_USERNAME=<your bot's @username, no @>
  CHAPA_API_KEY=<blank is fine for a manual-only launch>
  ```

  Values that depend on step 5's subdomains (fill in once step 5 is done,
  or now if you're keeping the subdomain names from `deploy/cloudflared/
  config.yml.example` as-is):
  ```
  MINIAPP_URL=https://app.arada.fun
  PUBLIC_BASE_URL=https://bot.arada.fun
  PAYMENTS_PUBLIC_BASE_URL=https://pay.arada.fun
  ```

  Your own admin IP(s), comma-separated (find yours with `curl -4
  ifconfig.me`) — **do not leave this blank in production**, an empty
  allowlist means unrestricted:
  ```
  ADMIN_IP_ALLOWLIST=<your real IP(s)>
  ```

  The withdrawal/deposit threshold values (`MIN_DEPOSIT_ETB`,
  `AUTO_APPROVE_WITHDRAW_ETB`, etc.) already have real defaults filled in
  from this session's own business-parameter answers — leave them unless
  you want different numbers.

- [ ] **5. Set up the Cloudflare Tunnel** (README's own "Domain and
  Cloudflare Tunnel" section has the full explanation; commands only,
  here, on the Proxmox VM)
  ```bash
  # Install cloudflared (Cloudflare's own instructions for your OS):
  # https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

  cloudflared tunnel login          # opens a browser, authorize your Cloudflare account
  cloudflared tunnel create jobingo # prints a tunnel id and writes a credentials JSON

  cp deploy/cloudflared/config.yml.example deploy/cloudflared/config.yml
  # edit deploy/cloudflared/config.yml: replace <TUNNEL_ID> with the id just printed

  # Copy the credentials file cloudflared just created to where
  # docker-compose.prod.yml expects it:
  cp ~/.cloudflared/<TUNNEL_ID>.json deploy/cloudflared/tunnel-credentials.json

  cloudflared tunnel route dns jobingo app.arada.fun
  cloudflared tunnel route dns jobingo admin.arada.fun
  cloudflared tunnel route dns jobingo pay.arada.fun
  cloudflared tunnel route dns jobingo bot.arada.fun
  ```
  Nothing to click in the Cloudflare dashboard beyond authorizing the CLI
  login above — the four `route dns` commands create the real DNS
  records directly.

- [ ] **6. First deploy**
  Push to `main` (CI runs, then CD deploys once it's green — pauses for
  your approval first if you did step 3). Or run it by hand the first
  time to watch it closely:
  ```bash
  cd deploy
  docker compose -f docker-compose.prod.yml up -d
  docker compose -f docker-compose.prod.yml ps   # everything healthy?
  ```

- [ ] **7. Verify it's actually reachable**
  ```bash
  curl -s https://app.arada.fun/healthz
  curl -s https://admin.arada.fun/healthz
  ```
  Both should return `{"status":"ok"}`. Then create your first real admin
  account (there's no self-registration path, on purpose):
  ```bash
  docker compose -f docker-compose.prod.yml exec admin \
    python -m services.admin.create_admin_cli --username <you> --role superadmin
  ```
  Prompts for a password (never a CLI argument — it'd land in shell
  history and be visible in `ps` to anyone else on the box), then prints
  a TOTP secret **shown only once** — scan it into an authenticator app
  immediately. Open `https://admin.arada.fun/console` and log in with
  that username/password/TOTP code.

- [ ] **8. Configure at least one real manual payment destination**
  Log into the admin console (`admin.arada.fun`, `payments:configure`
  role needed — superadmin) → **Payment Destinations** → add the real
  bank/Telebirr account players should send deposits to. This is the one
  step in this whole checklist that's a genuine business decision, not
  an engineering one — only you know the real account details.

- [ ] **9. A real end-to-end dry run before opening to the public**
  With a small real amount: register through the bot → submit a manual
  deposit with a real reference → approve it as admin → confirm the
  balance updates in the Mini App → play a round → (win or lose is fine,
  the point is the round settles) → submit a manual withdrawal → approve
  and settle it as admin → confirm funds actually move. This is the same
  lifecycle `tests/integration/test_miniapp_wallet_e2e.py`'s own capstone
  test proves in the sandbox — the point of doing it for real here is
  proving the *real* Cloudflare Tunnel, *real* domain, and *real* Telegram
  webhook all actually work together, which nothing in this sandbox could
  verify for you.

- [ ] **10. (Not an engineering checklist item — flagging, not blocking)**
  Legal/regulatory approval to operate real-money gambling in your
  jurisdiction. Entirely outside what this session can assess.
