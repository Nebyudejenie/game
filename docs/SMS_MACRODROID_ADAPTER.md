# SMS Control Plane — MacroDroid Delivery Node Adapter

Physical Android/MacroDroid setup for one **delivery node** in the
Enterprise SMS Control Plane (see `docs/SMS_CONTROL_PLANE.md` for the
architecture). MacroDroid here is **only one interchangeable caller** of
the generic delivery-node protocol — nothing on the server knows or cares
that MacroDroid is involved; any HTTP client that speaks the same four
calls (heartbeat / fetch-job / start / result) is a valid node.

**Honesty note, per this project's own testing discipline:** the macro
structure below follows MacroDroid's documented trigger/action set, but
has not been verified end to end against a physical device from this
environment (no device is available here) — treat it as a real, buildable
starting recipe for the technician setting up the phone, and confirm the
first delivery for real before relying on it.

## Before you start, you need

- [ ] A dedicated Android phone with a real, active SIM able to send SMS.
- [ ] Stable internet (mobile data or Wi-Fi) — the phone calls the server
      over HTTP even though it sends the actual message over the cellular
      SMS network.
- [ ] MacroDroid installed, with the same battery-unrestricted /
      autostart setup as `docs/TELEBIRR_MACRODROID_QUICK_SETUP.md` Step 1
      (steps 1–7 there apply identically here — a killed background app
      is the most common real-world failure mode for any always-on macro).
- [ ] A registered node and its one-time credential: ask an admin to
      register this device at **SMS Console → Delivery Nodes → Register
      node**, giving it a name (e.g. `android-shop-01`). The raw token is
      shown exactly once at registration (and again if rotated) — copy it
      immediately into a password manager or secure note. An admin must
      then click **Approve** before this node is given any work.
- [ ] The real base URL: `https://sms.arada.fun` (production) or
      whatever `--reload` URL a developer is testing against locally.

## Step 1 — Macro A: poll for work and send it

```text
MacroDroid
  → tap "+" (Add Macro)
  → Trigger
      → Regular Interval (or "Day/Time" repeating) → every 15–30 seconds
  → Action
      → Connectivity → HTTP Request
          → Method: POST
          → URL: https://sms.arada.fun/v1/nodes/fetch-job
          → Headers: Authorization = Bearer <NODE_TOKEN>
          → Store response in variable: fetch_response
  → Action
      → Dictionary/JSON → parse [fetch_response] into variables:
          job_message_id, job_phone, job_body
          (MacroDroid's JSON-path actions, or a plugin such as
          "JSON Reader", read `.job.message_id` / `.job.phone_e164` /
          `.job.body`; if `.job` is null, stop the macro here — no work.)
  → Constraint / IF block
      → IF [job_message_id] is not empty:
          → Action → Phone → Send SMS
              → Number: [job_phone]
              → Message: [job_body]
          → Action → Connectivity → HTTP Request
              → Method: POST
              → URL: https://sms.arada.fun/v1/nodes/jobs/[job_message_id]/start
              → Headers: Authorization = Bearer <NODE_TOKEN>
  → Save Macro, name it "SMS node -- poll and send", toggle ON
```

## Step 2 — Macro B: report the real result

Android reports whether an SMS actually sent via its own sent/delivery
broadcast — MacroDroid exposes this as its own trigger category. Use it
to report a *real* outcome rather than assuming success the instant
Macro A fires the send action:

```text
MacroDroid
  → tap "+" (Add Macro)
  → Trigger
      → Messaging → SMS Sent (or "SMS Sent/Delivered" depending on your
        MacroDroid version)
  → Action
      → Connectivity → HTTP Request
          → Method: POST
          → URL: https://sms.arada.fun/v1/nodes/jobs/[job_message_id]/result
          → Headers:
              Authorization = Bearer <NODE_TOKEN>
              Content-Type  = application/json
          → Body (raw/JSON):
              { "outcome": "delivered" }
            -- or, if the "SMS Sent" trigger reports a failure result:
              { "outcome": "failed", "error_class": "temporary" }
  → Save Macro, name it "SMS node -- report result", toggle ON
```

`job_message_id` must carry over from Macro A to Macro B — MacroDroid
persists variables across macros only if declared as **Global**
variables (not **Local**). Declare `job_message_id` as Global in Macro
A's variable settings, or this reporting step cannot know which message
just completed.

**If your MacroDroid version has no reliable sent/delivery trigger:** it
is more honest to report `{"outcome": "unknown"}` immediately after the
send action than to guess `delivered`. An `unknown` outcome is a real,
first-class state on the server (see `docs/SMS_CONTROL_PLANE.md`'s
retry/reconciliation section) — it is retried safely, never silently
dropped, and never double-counted as both unknown and delivered. Guessing
`delivered` when you don't actually know is worse: it can permanently
hide a real delivery failure.

## Step 3 — Heartbeat

```text
MacroDroid
  → Trigger → Regular Interval → every 60 seconds
  → Action → Connectivity → HTTP Request
      → Method: POST
      → URL: https://sms.arada.fun/v1/nodes/heartbeat
      → Headers: Authorization = Bearer <NODE_TOKEN>
                 Content-Type = application/json
      → Body: {"app_version": "macrodroid-1.0"}
  → Save Macro, name it "SMS node -- heartbeat", toggle ON
```

Heartbeat recency directly feeds the node's health score shown in the
console (**Delivery Nodes** screen) — a node with no heartbeat in the
last 5 minutes is scored 0 regardless of its past delivery history.

## What each fetch-job response means

| You see | Meaning | What to do |
|---|---|---|
| `{"job": null, "reason": "no queued messages"}` | Nothing to send right now. | Nothing — normal, expected most of the time. |
| `{"job": null, "reason": "node status is 'pending', not accepting work"}` | An admin hasn't approved this node yet. | Ask an admin to approve it in the console. |
| `{"job": null, "reason": "node status is 'disabled'/'draining', ..."}` | An admin intentionally paused this node. | Expected if someone disabled/drained it on purpose; ask before re-enabling. |
| `{"job": {...}}` | Real work — send it. | Follow Step 1/2 above. |
| HTTP 401 | Invalid or revoked credential. | Ask an admin to check the node's status or issue a new token (rotate-token). A revoked node cannot be un-revoked — register a new one instead. |

## Never do this

- Never hardcode the node token into campaign logic or share one token
  across multiple physical devices — each device gets its **own**
  credential so it can be revoked individually without affecting others.
- Never modify `[job_body]` before sending it — the server already
  rendered the final message text (template variables substituted); the
  device's only job is to send exactly what it was given.
- Never report `delivered` for a message this device didn't actually
  attempt to send.
