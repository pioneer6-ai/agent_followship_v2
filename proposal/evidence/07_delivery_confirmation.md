# Deployment evidence 7 - Delivery confirmation

Captured: 2026-09-25T09:47:34+00:00

## What this proves

Evidence 6 shows the provider accepted a message. This artifact shows
what happened to it afterwards, from two independent vantage points: the
provider's own delivery telemetry and the recipient mailbox. These are
different claims and are kept separate on purpose.

## The message under test

- Message id (as returned by SES): `010e01a0d7e70489-3cadc714-db2a-4219-9174-efc749f3ae5f-000000`
- Recipient: `martinchenonly1@gmail.com`
- Sent at: 2026-09-25T09:30:36+00:00

## Check 1 - provider delivery telemetry

CloudWatch `AWS/SES` for configuration set `patient-followup`, namespace queried in `ap-southeast-1`:

| Metric | Sum |
| --- | ---: |
| Delivery | 6 |
| Bounce | 0 |
| Complaint | 0 |

## Check 2 - recipient mailbox

- Method: IMAP `imap.gmail.com`, read-only search of the recipient account
- Mailbox searched: `martinchenonly1@gmail.com`
- Subject searched: "delivery confirmation"

| Folder | Messages found |
| --- | ---: |
| INBOX | 0 |
| spam | 5 |
| all_mail | 0 |

The message was found in: **spam**.

**It was delivered, but not to the inbox.** The mailbox
accepted the message and filed it as spam. This is the single
most consequential operational finding in this pack: a
reminder that lands in a spam folder has not reminded
anyone, and no amount of agent correctness compensates for
it. It is a sending-identity problem, not a software
problem -- see the deliverability item in the proposal's
risk table.

> Placement is not uniform, and this report is not the last word on it. Evidence 8
> records three later messages from the same system landing in the inbox. The two
> observations concern different messages at different times; both are reported.

Message headers observed:

```
Subject: Patient Follow-up Agent - delivery confirmation
From: brightsmileagent@gmail.com
Date: Fri, 25 Sep 2026 09:30:11 +0000
Message-ID: <010e01a0d7e6a32e-de7255b4-a647-42f4-8e67-7c915e659186-000000@ap-southeast-1.amazonses.com>
```

Message headers observed:

```
Subject: Patient Follow-up Agent - delivery confirmation
From: brightsmileagent@gmail.com
Date: Fri, 25 Sep 2026 09:30:27 +0000
Message-ID: <010e01a0d7e6e250-35534990-542e-4545-8385-16806cc2153c-000000@ap-southeast-1.amazonses.com>
```

Message headers observed:

```
Subject: Patient Follow-up Agent - delivery confirmation
From: brightsmileagent@gmail.com
Date: Fri, 25 Sep 2026 09:30:36 +0000
Message-ID: <010e01a0d7e70489-3cadc714-db2a-4219-9174-efc749f3ae5f-000000@ap-southeast-1.amazonses.com>
```
