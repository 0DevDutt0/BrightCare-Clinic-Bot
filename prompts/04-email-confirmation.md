# Phase 4 — email confirmation

## Instruction given

As with Phases 2 and 3, no full prompt was written. The scope came from the Phase 1
layout note:

> `services/email_service.py`   # Phase 4: interface + NotImplementedError stub

the SMTP settings fixed in Phase 1 (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, `FROM_EMAIL`, `FROM_NAME`, and `settings.email_configured`), and:

> go ahead with phase 4 from the outline

Assumptions recorded below.

## What was built

`SmtpEmailService` over stdlib `smtplib` and `email`. No dependency added — none is
needed. Wired into the final step of the booking flow, after the calendar event exists.

The loose end flagged at the end of Phase 3 is closed: the reply used to promise
"a confirmation email will follow shortly" while nothing sent one. It now reports what
actually happened.

## Assumptions made

1. **Message construction is a pure function.** `build_confirmation_message` takes
   values and returns an `EmailMessage`. The part most worth testing — headers, both
   body alternatives, the attachment — needs no mail server; only delivery is patched.

2. **Delivery runs in a thread.** `smtplib` is synchronous and would otherwise block
   the event loop for the second or two a Gmail handshake takes, stalling every other
   conversation. Same treatment as google-auth's token refresh in Phase 3.

3. **The email is sent inline, not fire-and-forget.** It costs the user ~1–2s before
   the "Booked." reply, but means that reply can state truthfully whether the
   confirmation went out. A background send would have to either lie or send a second
   message later.

4. **A failed email never rolls back the appointment.** The event is the clinic's
   record; the mail is the patient's. They fail independently, and the reply says which
   happened:
   - sent → "I've sent a confirmation to your email."
   - failed → "I couldn't send the confirmation email just now, but your appointment is
     booked and we'll see you then."

5. **An `.ics` attachment is included.** This goes beyond a literal reading of "email
   confirmation", and it is the one addition worth defending: a service account cannot
   add the patient as a calendar attendee (Phase 3's 403), so this file is the *only*
   route the appointment has into the patient's own calendar. `METHOD:PUBLISH`, not
   `REQUEST` — a copy to save, not an invitation nobody can answer. Removing it is a
   two-line change if it is unwanted.

6. **Both TLS modes are supported.** Port 465 uses implicit TLS (`SMTP_SSL`); anything
   else uses `STARTTLS`. Calling `starttls` on an already-encrypted SMTPS connection is
   an error, so the branch is not optional.

7. **`SMTPAuthenticationError` gets its own message** naming the App Password. Gmail
   rejecting an account password is by far the most likely misconfiguration, and it
   deserves the most specific error.

8. **`DisabledEmailService` raises rather than silently succeeding**, so an unconfigured
   deployment tells the patient no email is coming instead of promising one that never
   existed. `settings.email_configured` picks between the two at startup, exactly as
   the Phase 1 spec anticipated — no configuration change was needed.

9. **The recipient address is never logged.** Only its domain is, which keeps the log
   useful for diagnosing SMTP problems without recording patient data.

## Verified end to end

Real Groq, real Google Calendar, real Gmail SMTP, Friday 2026-09-04 14:08 IST:

```
USER  can I book Tuesday at 11am?
BOT   Tuesday 8 September at 11:00 AM is free. It runs 30 minutes. Shall I book it?  [2 calls]
USER  yes please
BOT   Lovely. What email address should I send the confirmation to?                  [0 calls]
USER  devduttshoji123@gmail.com
BOT   Thanks. To confirm: Tuesday 8 September at 11:00 AM at 12 Orchard Rd ...       [0 calls]
USER  yes
BOT   Booked. Your appointment is Tuesday 8 September at 11:00 AM at 12 Orchard Rd.
      I've sent a confirmation to your email.                                        [0 calls]
```

The event appeared on the real calendar and the email was accepted by Gmail SMTP —
the first exercise of the App Password. The test event was deleted afterwards; the
email was left in the inbox.

## Environment-variable shadowing, found afterwards

Two **User-scope Windows environment variables**, `TELEGRAM_BOT_TOKEN` and
`GROQ_API_KEY`, outrank the `.env` file in pydantic-settings' precedence order. Every
local run had silently been using those rather than the values in `.env`, which is why
the startup log kept reporting the wrong bot.

The precedence itself is correct and must stay — it is how Render and Railway will
inject configuration in Phase 5. The local workaround is to clear them for the process:

```bash
env -u TELEGRAM_BOT_TOKEN -u GROQ_API_KEY ./venv/Scripts/python -m uvicorn app.main:app --port 8000
```

They were deliberately not deleted: that `TELEGRAM_BOT_TOKEN` belongs to another
project on the same machine.

## Out of scope, still

Deployment (Phase 5): `RUN_MODE=webhook`, `PUBLIC_BASE_URL`, a host, and whatever the
CI/CD pipeline in `GitHub_repo_Link` is meant to do. Nothing has been pushed to the
remote yet.
