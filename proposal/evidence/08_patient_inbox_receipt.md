# Deployment evidence 8 - Patient inbox receipt

Captured: 2026-09-26T10:36+08:00 — screen capture contributed by the operator.

## What this proves

Reminders written and sent by this agent through the configured SES identity reached the recipient's **inbox**, rendered as ordinary mail from "BrightSmile Dental", with the per-patient generated wording intact. It is a third vantage point on delivery alongside the provider telemetry and the IMAP search in evidence 7, and it disagrees with evidence 7 on where the mail was filed. Two limits travel with the image: it was supplied by the operator rather than captured by a script, so it cannot be re-derived, and its recipients are the project's own sample patient list, which resolves to a single allow-listed mailbox. No real patient appears in it.

## The artifact

| | |
| --- | --- |
| File | `proposal/evidence/screenshots/patient_inbox_received.png` |
| Size | 365,876 bytes |
| Dimensions | 2,840 x 1,538, 8-bit RGBA PNG |
| SHA-256 | `6302c9b8941f048ede4c527671513d48be1188bfa469c5c5e0e4bd94ea70a2c9` |
| Subject | A webmail client with the search scoped to "Dental Appointment Reminder" |
| Thread header | `9月25日周五 19:18–19:24 (15小时前)` — Friday 25 September, spanning 19:18 to 19:24, "15 hours ago" |

## What the image shows

Three messages in one thread, every sender line reading **BrightSmile Dental**. The
first two are collapsed to their opening line, the third is expanded; a bin
affordance is hovering over the first.

| Order | Opening line as displayed | Recipient addressed |
| --- | --- | --- |
| 1 | `[DOCTOR-EDITED] Dear Patient 01, this is Dr. Lee from BrightSmile. Your cleaning is well overdue - I have reserved Tuesday 3pm for you. Please reply to confirm.` | Patient 01 |
| 2 | `Hi Patient 20, our records show your dental cleaning is now 200 days past the date we recommended. We'd like to help you get back on track as soon as possible.` | Patient 20 |
| 3 | `Hi Patient 21, we've missed you at the clinic. Your cleaning is now 200 days overdue, and we don't want your dental health to wait any longer. Please call us today to book your cleaning. Warm regards, the care team.` | Patient 21 |

The three bodies differ from one another in greeting, phrasing and closing. That is
the point of interest: these are not one template with a substituted name, and none
of the three sentences appears in any template in the repository.

## What corroborates it

**The proposal already documents this trial.** Section 9.4 records it: the dashboard
with a model configured for both decisioning and message authoring, run against a
cohort of 21 patients all scored `critical`; the agent drafted a reminder for each; a
staff member edited one draft in the review panel; "Confirm Selected" then claimed
the batch and delivered it, `21 sent, 0 failed, queue empty`. This image is the
recipient side of that batch — the same event, seen from the mailbox instead of from
the operator's console.

**The run's audit log puts the same messages in the same window.** The log for that
deployment (the sibling clone's append-only `audit_log.json`; section 9.4 explains
why it is deliberately not a hash-identified member of this pack) holds:

| Timestamp (+08:00) | Patient | Channel | Direction | Success | Preview |
| --- | --- | --- | --- | --- | --- |
| 2026-09-25T19:18:10 | CRIT001 | email | outbound | true | `[DOCTOR-EDITED] Dear Patient 01, this is Dr. Lee from BrightSmile. Your cleaning is well overdue - I…` |
| 2026-09-25T19:23:03 | CRIT001 | email | outbound | true | `[DOCTOR-EDITED] Dear Patient 01, this is Dr. Lee from BrightSmile. Your cleaning is well overdue - I…` |
| 2026-09-25T19:24:06 | CRIT020 | email | outbound | true | `Hi Patient 20, our records show your dental cleaning is now 200 days past the date we reco…` |
| 2026-09-25T19:24:08 | CRIT021 | email | outbound | true | `Hi Patient 21, we've missed you at the clinic. Your cleaning is now 200 days overdue, and…` |

The first line is a single send of the edited draft, five minutes before the batch;
the batch itself is 21 sends to `CRIT001`–`CRIT021` between 19:23:03 and 19:24:08, all
`channel=email`, all `direction=outbound`, all `success=true` — which is section
9.4's "21 sent, 0 failed". Gmail has grouped three of them into one thread because
they share a subject and a sender.

Two cross-checks identify the log as that run rather than a similar one. It carries
exactly the 11 `llm-error` and 1 `llm-guardrail` entries section 9.4 quotes. Its
`Decided by: llm` count now reads 1,600 where section 9.4 says 944 — the record is
append-only and has grown since that section was written, which is precisely the
property that keeps it out of the pack.

Two further cross-checks, both recomputable from files in this repository:

- `CRIT001`, `CRIT020` and `CRIT021` are the ID column of
  `test_data/demo_21_critical_patients.csv`: 21 rows named `Patient 01` to
  `Patient 21`, every one carrying `martinchenonly1@gmail.com` — one of the two
  addresses in the `AWS_EMAIL_ALLOWED_ADDRESSES` allow-list of evidence 2. That is
  why three patients share one mailbox and one thread.
- The "200 days" in the body is not decorative. Those rows record a last visit of
  2026-02-23 and a recall window of 14 days, so the cleaning fell due on 2026-03-09;
  on 25 September 2026 that is exactly 200 days overdue.

## What it does not prove

> The pack's other seven reports were each written by a capture script. This image
> was not: nobody can re-run it, and the SHA-256 above pins the bytes this pack
> ships, not the provenance of the screen it records.

- **That the doctor-edited marker is generated.** The string `[DOCTOR-EDITED]`
  appears nowhere in the repository — a case-insensitive search of the revision
  returns no source file. It is text the operator typed into the draft body before
  confirming the send, carried through the draft-edit path
  (`agent/orchestrator.py` `update_pending_send`, `edited_by`, reached from
  `web/app.py`), not a label the software emits. Do not read it as an audit feature.
- **That any real patient was contacted.** The recipients are the sample list. There
  is no live patient behind any of these three messages.
- **That the corroborating log can be checked from inside the pack.** The send record
  quoted above lives in the sibling clone's append-only log, which section 9.4 keeps
  out of the pack on the grounds that it is still being written to. A reviewer can
  check the arithmetic in this report against `test_data/demo_21_critical_patients.csv`
  and the cited proposal sections, but cannot recompute the timestamps or previews
  from a file this pack ships.
- **That the message arrived for anyone else, or anywhere else.** One mailbox, one
  provider, three of 21 messages shown. This is an existence proof of inbox
  placement, not a placement rate.
- **That a delivered reminder is a read reminder.** An inbox is not an open, a reply
  or a booking. Nothing here shows a patient acting on an email.
- **That evidence 7 was wrong.** Evidence 7 reports a read-only IMAP search that
  found its subject fragment in spam. Both observations can hold: they concern
  different messages at different times, and placement for a young sending identity
  on a free mailbox provider is exactly the kind of thing that varies. The
  reputation and domain work in the proposal's risk table is what makes placement
  consistent, and this artifact does not reduce the need for it.
- **That this capture sits inside the pack's own capture window.** The scrollback
  timestamp is 19:18–19:24 on 25 September (+08:00), i.e. 11:18–11:24 UTC, whereas
  every scripted capture in this pack ran between 09:02 and 09:47 UTC that day. This
  is a later event, observed after the fact, not a re-render of a capture.

## Provenance of this image

- Contributed by the operator as a screenshot file, copied into the pack unchanged
  (byte-for-byte; the SHA-256 above is the same digest the supplied file carried).
- Taken ~15 hours after the send, which is 10:18–10:24 on 26 September; the file's
  own timestamp is 10:36 the same morning. The thread's relative age label and the
  file's timestamp agree, which is a consistency check on the image's own clock —
  not independent confirmation of its contents.
- The corroborating audit log lives in the sibling clone used for this pilot, not in
  this pack; section 9.4 of the proposal gives the reason it is kept out. For the
  record, the file read for this report was
  `/Users/martinchen/agent_followship_v3/audit_log.json`, 1,781,289 bytes, SHA-256
  `0b2023c05ba2924ff1ff685c6f0422f0a31e164787ad9f732ad7d28166ac5b8f`, last written
  2026-09-25T19:44+08:00. That identifier is offered as a description of what was
  read, not as a hash a reviewer can check against a file in the pack.
