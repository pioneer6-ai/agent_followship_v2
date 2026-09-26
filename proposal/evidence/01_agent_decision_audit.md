# Deployment evidence 1 - Agent decisions and audit trail

Captured: 2026-09-25T09:02:41+00:00

## What this proves

The agent runs its full Perceive -> Decide -> Act -> Observe loop and records a durable, queryable audit trail for every decision and every delivery attempt. The counts below are computed directly from the artifact whose hash is recorded here, so they can be re-derived by a reviewer instead of taken on trust.

## Scope (what this does *not* claim)

The agent's default delivery backend prints instead of transmitting; real sends require MESSAGING_DRY_RUN=0 AND AGENT_LIVE_SENDS=1. These events therefore evidence the agent's decisions and its audit trail, not message transmission.

## Artifact provenance

- Live file: `audit_log.json` (append-only; read via the snapshot below)
- Snapshot analysed: `proposal/evidence/artifacts/audit_log_snapshot.json`
- SHA-256: `76551f7389296eda3ff1d905ec5ed8c486f98fccd12cff0db80e9cd9bff06644`
- Size: 261,297 bytes
- Events: 493
- Entry timestamps (audit logger clock): 2026-09-25T17:02:28.137398 to 2026-09-25T17:02:28.732906
- Patients: 11 distinct patient identifiers

> Timestamp note: the audit logger stamps entries from its own clock, not from the agent's injected clock. Under the fixed-clock harness used to produce this record that means the timestamps show a sub-second wall-clock window while the decisions they describe span the simulated period above. In production the two coincide, because both are the system clock; the simulated period is the meaningful span for reading this record.

> Data-hygiene note: `audit_log.json` is append-only and the automated test
> suite writes to it (tests exercise the real orchestrator and its audit
> logger), so the live file grows every time the tests run. The snapshot
> above was frozen at capture time and is the artifact these figures come
> from; re-running the tests will not reproduce identical totals from the
> live file. See the robustness section of the proposal.

## How this record was produced

The log was not hand-written: it is the output of the real orchestrator driven by `proposal/scripts/generate_audit_record.py` over the project's sample patients, with the clock fixed so the run is independent of the wall clock.

- Simulated period: 2026-09-21 to 2026-11-01 (42 daily cycles)
- Delivery calls handled offline: 37
- Decisions by action: send_reminder 22, escalate_to_staff 11, do_nothing 430

### Injected patient replies (the Observe path)

Each reply was fed to the agent's reply handler so the record also covers what happens when a patient answers, rather than only outbound decision-making.

| Day | Patient | Reply | Case status before | Case status after |
| --- | --- | --- | --- | --- |
| 2026-09-23 | P006 | Yes, I'd like to schedule an appointment | message_sent | booked |
| 2026-09-30 | P007 | No thanks, not needed right now | message_sent | declined |
| 2026-10-07 | P004 | How much will this cost with my insurance? | escalated | escalated |
| 2026-10-14 | P003 | Can I come in next week? | escalated | awaiting_reply |
| 2026-10-21 | P011 | Yes please | None | no_case |

> Generator note: Channels were the offline printing backend, asserted at both ends: every constructed channel had to be a PrintDeliveryBackend, and the transmitting backend's deliver() was patched to raise. That is what makes this a record of decisions instead of transmission. The sample patients carry invented addresses, so the run must not transmit; real transmission is evidenced by capture_live_send.py.

Run summary frozen as `proposal/evidence/artifacts/audit_log.generation.json` (SHA-256 `42da6d67abc3fdad5415daab3e377b2b44c9c3531acad79079b20df89eb59243`).

## Agent decisions

463 decisions were recorded.

### Action taken

| Action | Count |
| --- | ---: |
| do_nothing | 430 |
| send_reminder | 22 |
| escalate_to_staff | 11 |

### Case urgency at decision time

| Urgency | Count |
| --- | ---: |
| critical | 209 |
| high | 150 |
| medium | 78 |
| low | 26 |

### Case status at decision time

| Case status | Count |
| --- | ---: |
| escalated | 282 |
| message_sent | 98 |
| booked | 39 |
| declined | 32 |
| pending | 11 |
| awaiting_reply | 1 |

### Escalation to human staff

- Escalation actions: 11
- Distinct patients ever escalated: 9
- Escalation rate: 2.4% of decisions

## Communication attempts

30 attempts (26 outbound, 4 inbound patient replies).

| Channel | Attempts | Succeeded | Failed | Success rate |
| --- | ---: | ---: | ---: | ---: |
| email | 12 | 12 | 0 | 100.0% |
| sms | 9 | 9 | 0 | 100.0% |
| whatsapp | 7 | 7 | 0 | 100.0% |
| phone_call | 2 | 2 | 0 | 100.0% |

Overall success rate: **100.0%** (30 of 30).

### Reading the success rate

Every attempt is recorded as successful because the delivery backend is the offline printer: it accepts any recipient it is handed and so cannot fail. The rate is therefore a property of the offline backend, **not** carrier deliverability, and it is not comparable to the fallback-path behaviour described in the proposal's robustness section. Carrier-level delivery is evidenced separately, by the live-send and delivery-telemetry captures.

