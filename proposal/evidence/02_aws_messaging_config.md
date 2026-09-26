# Deployment evidence 2 - AWS messaging configuration

Captured: 2026-09-25T09:30:27+00:00

## What this proves

The messaging transport is wired to named AWS services in a named
region, it sends from a verified identity, and it refuses recipients
outside an explicit allow-list. The last section pins every claim to a
file and line in the deployed revision.

## Configuration the deployment runs with

| Setting | Value |
| --- | --- |
| `AWS_REGION` | `ap-southeast-1` |
| `AWS_SES_SOURCE` | `brightsmileagent@gmail.com` |
| `AWS_SES_CONFIGURATION_SET` | `patient-followup` |
| `AWS_SMS_ORIGINATION_IDENTITY` | `+6585144321` |
| `AWS_SMS_ALLOWED_NUMBERS` | `+6583536885` |
| `AWS_EMAIL_ALLOWED_ADDRESSES` | `martinchenonly1@gmail.com,rgjh1996@gmail.com` |
| `MESSAGING_DRY_RUN` | `0` |
| `AGENT_LIVE_SENDS` | `<empty>` |

## Live AWS read-back

- AWS session: **authenticated**
- SES sending enabled: `True`
- SES 24-hour quota: `200.0`, max send rate `1.0/s`, sent in last 24h `3.0`

Verified SES identities:

- `martinchenonly1@gmail.com`
- `rgjh1996@gmail.com`
- `brightsmileagent@gmail.com`

Verified SMS destination numbers:

- **Not readable**: `aws: [ERROR]: An error occurred (SubscriptionRequiredException) when calling the DescribeVerifiedDestinationNumbers operation: The AWS Access Key Id needs a subscription for the service`

> A quota of 200 messages per 24 hours and 1 message per second is
> the SES sandbox ceiling, not a production limit. Production access
> and SMS origination identity onboarding are prerequisites for a
> real patient population and are currently outstanding.

> The SMS read failed with `SubscriptionRequiredException`, which
> means this account is not subscribed to End User Messaging SMS in
> the queried region. The SMS code path is complete, but attempts
> return `not_subscribed` until the service is provisioned. Do not
> describe SMS as available on the strength of this pack.

## Delivery code paths in this revision

| File | Locator | Line |
| --- | --- | ---: |
| `tools/aws_providers.py` | `client.send_text_message` | 307 |
| `tools/aws_providers.py` | `client.send_email` | 348 |
| `tools/messaging.py` | `def send_sms` | 383 |
| `tools/messaging.py` | `def send_email` | 413 |
| `agent/delivery.py` | `def is_configured_for_live_sends` | 334 |
