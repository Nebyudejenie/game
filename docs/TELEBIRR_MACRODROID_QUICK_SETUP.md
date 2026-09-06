# Telebirr MacroDroid Quick Setup

Physical Android/MacroDroid setup only. For everything else about this
system, see `docs/TELEBIRR_SMS_OPERATIONS_GUIDE.md`. Give this page to
the technician setting up the dedicated payment phone; nothing else is
required for that specific job.

## Before you start, you need

- [ ] A dedicated Android phone (this phone's only job is receiving
      Telebirr SMS for this system — do not use a personal phone).
- [ ] A real, active SIM able to receive SMS, registered to the Telebirr
      account that will receive/send the payments this system tracks.
- [ ] A charger, kept permanently plugged in.
- [ ] Stable internet (mobile data or Wi-Fi).
- [ ] The real ingestion URL: `https://payments.arada.fun/internal/telebirr/ingest`
      — a dedicated production payment hostname, fully live end to end
      (DNS, Cloudflare Tunnel, Traefik, the real payments service),
      confirmed 2026-09-05 with real external requests: missing token →
      401, wrong token → 401, malformed body → 422.
- [ ] The real `MACRODROID_INGEST_TOKEN` value (get this from an admin —
      it is a secret, never write it anywhere other than this one macro's
      configuration). Confirmed configured in production as of 2026-09-05.

## Step 1 — Prepare the phone

1. Insert the SIM, confirm it has signal.
2. Connect to Wi-Fi and/or confirm mobile data works.
3. Set the screen lock to "None" or "Swipe" (removes one variable; a
   locked screen is not required to stop SMS receipt, but this keeps
   physical checks simple).
4. Install **MacroDroid** from the Google Play Store.
5. Open MacroDroid once, grant every permission it asks for, in
   particular:
   - SMS (Read SMS / Receive SMS)
   - Notifications (optional, but recommended so you can see macro
     activity)
6. Go to **Android Settings → Apps → MacroDroid → Battery** and set it to
   **Unrestricted** (not "Optimized", not "Restricted"). This single step
   is the most common real-world cause of "SMS arrives on the phone but
   the server never sees it" — Android silently kills background apps
   otherwise.
7. If this phone is Xiaomi (MIUI), Huawei (EMUI), Oppo (ColorOS), or Vivo
   (FuntouchOS): these manufacturers have an **additional**, separate
   "Autostart" or "Auto-launch" permission beyond standard Android battery
   settings. Find it under Settings → Apps → (manage apps) →
   Autostart/Auto-launch, and enable it for MacroDroid. The exact menu
   wording varies by OS version — search "[your phone brand] autostart
   permission" if you can't find it, or check MacroDroid's own support
   pages for the current list.

## Step 2 — Build the macro

Open MacroDroid and follow this exact path:

```text
MacroDroid
  → tap "+" (Add Macro)
  → Trigger
      → Messaging
          → SMS Received
          → set "Message Content" filter → Contains →
            paste exactly: Your transaction number is
          → Save
  → Action
      → Connectivity
          → HTTP Request
          → Method: POST
          → URL: https://payments.arada.fun/internal/telebirr/ingest
          → Headers (add two):
              Authorization  =  Bearer <MACRODROID_INGEST_TOKEN>
              Content-Type   =  application/json
          → Body: switch to raw/JSON mode, enter exactly:
              {
                "raw_sms": "[sms_message]",
                "device_id": "<pick a fixed name for this phone, e.g. shop-till-android>"
              }
          → (optional) enable "Store response in variable" so you can see
            the result in MacroDroid's own log
          → Save
  → give the macro a name, e.g. "Telebirr SMS -> Ingest"
  → Save Macro
  → make sure the toggle at the top of the macro is ON
```

`[sms_message]` is MacroDroid's own built-in variable that holds the
complete text of whatever SMS just triggered the macro — select it from
MacroDroid's variable picker inside the Body field rather than typing it
by hand, so you don't mistype the variable name.

**Do not** try to extract just the reference or amount yourself and send
only that — the server needs the **complete original SMS text** to do its
own verification. Sending anything less will fail.

## Step 3 — Test it

1. Arrange for one real (or realistic-format) Telebirr SMS to arrive on
   this phone.
2. Watch MacroDroid's own log for the macro firing and the HTTP response
   code.
3. Expect **HTTP 200** with a JSON body containing `"status"`.
4. Ask whoever has admin/database access to confirm a new row appeared
   (or ask them to check the admin console's **Telebirr Evidence**
   screen for the reference from that SMS).

## What each response means

| You see | Meaning | What to do |
|---|---|---|
| HTTP 200, `"status": "ingested_available"` | Success — recipient matched, ready for a player to redeem. | Nothing — working as intended. |
| HTTP 200, `"status": "ingested_rejected"` | The SMS parsed fine, but its recipient doesn't match the configured Arada Bingo account. | Tell an admin — likely the recipient isn't configured yet, or this SMS is for a different account entirely. |
| HTTP 200, `"status": "duplicate"` | This exact SMS was already ingested. | Nothing — this is safe and expected if the macro somehow fires twice for one message. |
| HTTP 200, `"status": "unparseable"` | The server couldn't read a reference from this message at all. | Check the SMS is a real Telebirr payment confirmation, not something else that happened to contain the trigger phrase. |
| HTTP 401 | Wrong or missing bearer token. | Double check the `Authorization` header value for typos/extra spaces; confirm the token hasn't been rotated (ask an admin). |
| HTTP 503 | The server isn't configured to accept ingestion right now. | Tell an admin — this is a server-side configuration issue, not something fixable on the phone. |
| No response / timeout | Network issue on the phone, or the server is unreachable. | Check the phone's own internet connection first; if that's fine, tell whoever manages deployment the server may be down. |

## Retry on a failed request

If the HTTP Request action fails (no response, a timeout, or a non-200
your own network caused rather than the server rejecting the content),
the SMS itself is **not lost** — it stays in the phone's normal SMS
inbox like any other message; only the *forward-to-server* step failed.
MacroDroid's HTTP Request action has its own retry/timeout settings
(exact wording varies by MacroDroid version — look for "Timeout" and any
retry-count option in the action's own advanced settings when building
Step 2) — enable a short retry there if available. Whether or not an
automatic retry is configured, treat a "No response / timeout" result
(see the table above) as something to physically check on, not silently
ignore: if the server was briefly unreachable, the specific SMS that
failed may need a manual re-trigger (MacroDroid can usually re-run a
macro against a stored/older SMS, or the message can be manually
forwarded through the same macro once — check MacroDroid's own
documentation for "re-run macro" / "test with existing message" if this
comes up).

## Reboot survival

After any phone restart (a real reboot, not just screen lock/unlock),
explicitly verify the macro is still armed **before** trusting the phone
again — don't assume it:

1. Reboot the phone.
2. Open MacroDroid and confirm the macro's own toggle (Step 2's last
   line) is still **ON**. If MacroDroid failed to auto-start at all
   (rare, but possible if the Autostart/Auto-launch permission from Step
   1.7 didn't take effect), the toggle may show ON but nothing will
   actually fire — open the app itself at least once after every reboot
   to be sure it's genuinely running, not just configured to.
3. Send one real or test-format SMS and confirm the macro fires
   (Step 3's own test procedure) — this is the only way to be certain
   the phone is actually working again after a reboot, not an assumption
   from the toggle state alone.

Add this check to whatever routine covers "what to do after this phone
loses power or restarts" — a phone that silently stopped forwarding SMS
after a reboot is indistinguishable from a working one until someone
checks.

## Ongoing care

- Keep the phone charging at all times.
- Don't install unrelated apps that might trigger battery-optimization
  prompts affecting MacroDroid.
- After any Android system update, re-check Step 1.6/1.7 (OS updates
  sometimes silently re-enable battery restrictions).
- After any reboot, walk the "Reboot survival" checklist above.
- If the phone will be replaced or the SIM moved to a new device, repeat
  this entire guide on the new phone before decommissioning the old one.
- If the phone is lost or the token may have leaked, **stop** — do not
  keep using the old macro. Tell an admin immediately so the token can be
  rotated (see the main operations guide, §4).
