# Deployment evidence 6 - Live end-to-end delivery

Captured: 2026-09-25T09:30:36+00:00
Revision: `fbcc6c1ca09a7624fd5f01016e674cacdc4a5287`

## What this proves

A message was handed to the real provider through the agent's own tool
layer and the provider answered. This is the only artifact in the pack
that evidences transmission rather than behaviour, which is why it is
run only with explicit approval.

## The send

- Tool: `send_email`
- Channel: email
- Recipient: `martinchenonly1@gmail.com`
- Sender identity: `brightsmileagent@gmail.com`
- Reason recorded: "pilot acceptance check: verifying the delivery path end to end"

## Provider response

```json
{
  "status": "sent",
  "channel": "email",
  "recipient": "martinchenonly1@gmail.com",
  "data": {
    "reason": "pilot acceptance check: verifying the delivery path end to end"
  },
  "message_id": "010e01a0d7e70489-3cadc714-db2a-4219-9174-efc749f3ae5f-000000"
}
```

The provider accepted the message and returned message id
`010e01a0d7e70489-3cadc714-db2a-4219-9174-efc749f3ae5f-000000`. Acceptance is not delivery: for email,
the SES configuration set emits a separate `Delivery` event that is
the authoritative record of arrival.

## Tool audit trail

Every attempt the toolkit made during this run, including the reason:

```json
[
  {
    "timestamp": "2026-09-25T09:30:36+00:00",
    "tool": "send_email",
    "channel": "email",
    "recipient": "martinchenonly1@gmail.com",
    "status": "sent",
    "simulated": false,
    "message_id": "010e01a0d7e70489-3cadc714-db2a-4219-9174-efc749f3ae5f-000000",
    "error_code": null,
    "provider_code": null,
    "error_message": null,
    "latency_ms": 228,
    "reason": "pilot acceptance check: verifying the delivery path end to end",
    "template": null,
    "body_preview": "This message confirms that the Patient Follow-up Agent can deliver email through its tool layer.\n\nIt was sent during a pilot acceptance check. No action is nee…"
  }
]
```

## How to reproduce

```bash
.venv/bin/python proposal/scripts/capture_live_send.py --channel email --approve
```
