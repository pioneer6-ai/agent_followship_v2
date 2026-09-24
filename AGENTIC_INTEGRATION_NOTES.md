# Agentic Integration Notes — `feature/booking-agent`

*For Pioneer 6 teammates reviewing this branch. ~5 minute read.*

## 1. Purpose of This Branch

This branch integrates a set of **safety and follow-up lifecycle capabilities** from an earlier prototype into the team's existing V2 architecture. It does **not** replace V2.

V2's existing foundation is unchanged and remains the basis of the system:
- `RuleDecisionEngine` / `LlmDecisionEngine` (`agent/decision.py`) — still the decision layer
- The `tools/` messaging/provider stack (AWS SES/SNS, Meta WhatsApp, Twilio, SMTP)
- The staff dashboard (`web/app.py`, `web/templates/dashboard.html`)

What this branch adds is a **safety authorization layer** and a **temporal/lifecycle foundation** (clock, opt-out tracking, future re-triggering, booking-window limits) that sit around the existing decision/tool architecture, not inside it.

## 2. Architecture Before vs After

**Before:**
```
Trigger/Case → Decision Engine → Action → Tools
```

**After:**
```
TriggerService → Decision Engine → PolicyGuard → Tool Execution → Observation → State → Future Trigger
```

- **Trigger = WHEN** — `TriggerService` decides which cases deserve attention today, before any reasoning happens.
- **Decision engine = WHAT** — `RuleDecisionEngine`/`LlmDecisionEngine` (unchanged) propose an action.
- **PolicyGuard = WHETHER** — a deterministic, non-LLM layer that authorizes, denies, or overrides the proposed action. This is new.
- **Tools = HOW** — the existing `tools/` registry carries out the authorized action.
- **Observation/state = WHAT HAPPENS NEXT** — delivery outcomes and case status feed into `next_followup_at`, closing the loop back to Trigger.

## 3. Features Added / Integrated

| Feature | File(s) | Why it exists |
|---|---|---|
| `PolicyGuard` | `agent/policy_guard.py` | Deterministic, LLM-independent safety authorization between decide and execute. Adapted from the prototype for V2's single `AgentAction` vocabulary. |
| `Clock` / `SystemClock` / `FixedClock` | `core/clock.py` | Removes hardcoded/scattered `date.today()` calls; makes date-relative logic deterministic and testable. |
| `TriggerService` | `core/trigger_service.py` | Deterministic filter for "which cases are actionable today," separate from deciding what to do about them. **Phase 1 scope only** — see §7. |
| Opt-out tracking/enforcement | `core/models.py` (`PatientRecord.opted_out`, `CaseStatus.OPTED_OUT`), `agent/policy_guard.py` | Patients who ask to stop being contacted must have that respected permanently, not just for the message that triggered it. |
| Emergency signal handling | `agent/conversation.py` (`is_emergency_signal`) | Detects potential medical emergencies in patient replies so PolicyGuard can force escalation instead of continuing normal booking/reminder flow. |
| Ambiguous booking-consent protection | `agent/conversation.py` (`_is_qualified_affirmation`) | An affirmative word combined with a negation/reschedule/temporal qualifier must not be read as unconditional consent to book. |
| Configurable 7-day booking window | `core/config.py` (`booking_window_days`), `core/data_access.py`, `agent/action_handlers.py` | Bounds how far ahead a slot may be offered/booked, independent of (and narrower than) the existing 60-day safety cap. |
| `next_followup_at` | `core/models.py` (`FollowUpCase.next_followup_at`) | Lets a case be parked with an explicit date to re-check it, feeding `TriggerService`. |
| Terminal-state protection | `agent/orchestrator.py` | Prevents an inbound message from silently reopening a case that already reached `BOOKED`/`DECLINED`/`ESCALATED`/`OPTED_OUT`. |
| Future-trigger state carry-forward | `agent/orchestrator.py` (`_carry_forward`) | Ensures `next_followup_at` survives being rebuilt fresh from the data store on every daily cycle. |

## 4. Important Bugs Fixed

- **"Yes, but not this week"** previously risked being treated as unconditional booking consent (it matched an affirmative pattern and no decline pattern). Now: co-occurring qualifying language forces `consent_signal="ambiguous"` → `REQUIRE_CLARIFICATION`, and booking cannot proceed.
- **An opted-out patient could still receive a generic response.** After PolicyGuard denied a later action (e.g. a subsequent "yes, book it"), the orchestrator still sent `"Thank you for your response."` and let the status drift from `OPTED_OUT` to `AWAITING_REPLY`. Now: `DENY`/`DO_NOTHING` guarantees no outbound send, no tool execution, and no unintended state mutation — the case stays `OPTED_OUT`.
- **`next_followup_at` disappeared between daily cycles.** `_carry_forward` didn't propagate it onto the freshly-rebuilt case object each cycle, so a scheduled future check-in was silently lost. Now: preserved until reached (or replaced by a newly computed value), and explicitly cleared when a case reaches a terminal status.
- **Natural emergency phrasing was missed.** "I'm bleeding badly" didn't match the original keyword set (only "severe bleeding" did). Emergency detection was expanded to cover common variants (bleeding intensity/duration, breathing difficulty, swelling severity, pain intensity) in either word order, while ordinary mild-symptom mentions ("a little bleeding after brushing") remain non-emergency.

## 5. Safety Invariants

These guarantees now hold across the reply-handling path:

- PolicyGuard authorization happens **before** any side-effecting action (message send, booking, escalation).
- `DO_NOTHING` means **zero** operational side effects — no send, no tool call, no status mutation.
- Opted-out patients **cannot** be contacted again by the normal workflow, regardless of what a later message says.
- Terminal states (`BOOKED`/`DECLINED`/`ESCALATED`/`OPTED_OUT`) **cannot** be silently reopened by an inbound message.
- Ambiguous consent **cannot** cause a booking to execute.
- Emergency signals **override** ordinary workflow (including opt-out) via `FORCE_ESCALATION`.
- Booking/slot search **respects** the configured `booking_window_days`, not just the 60-day safety cap.

## 6. Current Workflow Examples

**Normal:**
```
Patient reply → signal extraction → proposed action → PolicyGuard → authorized action → tool → result → state
```

**Ambiguous:**
```
"Yes, but not this week" → ambiguous consent → REQUIRE_CLARIFICATION → no booking
```

**Opt-out:**
```
"Stop messaging me" → OPTED_OUT
  ↳ later "yes, book it" → DENY → DO_NOTHING (no send, no booking, status unchanged)
```

**Emergency:**
```
"I'm bleeding badly" → emergency signal → FORCE_ESCALATION (overrides any opt-out signal in the same message)
```

**Future trigger:**
```
next_followup_at set → intermediate daily cycles skip the case → trigger date reached → case becomes actionable again
```

## 7. What This Branch Does NOT Yet Include

This is important for reviewers to understand the actual scope. The following are **not implemented** on this branch:

- Patient Portal
- `/patient/<token>` patient-facing booking route
- Gemini Agent / `AgentState` / the older prototype's Gemini-based `ToolRegistry`
- The full `PENDING_FUTURE_AVAILABILITY` park-and-retry lifecycle
- "None of these times work" patient flow
- Real Google Calendar integration (calendar is mocked)
- Gmail API OAuth integration (SMTP config exists, no OAuth)
- Persistent database/state (data store and calendar are in-memory mocks)
- Full Agent Activity Timeline UI (no corresponding dashboard endpoint exists)

None of the above should be assumed present when reviewing or demoing this branch.

## 8. Existing V2 Components Preserved

Deliberately kept as-is, not replaced:
- `RuleDecisionEngine` (`agent/decision.py`)
- `LlmDecisionEngine` / existing Claude-based LLM integration
- The existing `tools/` messaging/provider stack (schemas, errors, result wrapper)
- AWS/provider integrations already in V2 (SES, SNS, Twilio, Meta WhatsApp)
- The staff dashboard (`web/app.py`, `web/templates/dashboard.html`)
- The existing booking architecture (`AppointmentScheduler`, `CalendarIntegration` interface) — extended with optional parameters, not rewritten

## 9. Validation

- **Baseline before integration:** 612 tests
- **Current passing test count (just re-run):** **650 passed**
- **Warnings:** 16 (all `DeprecationWarning` from `openpyxl`'s use of `datetime.utcnow()`, pre-existing and unrelated to this branch's changes) — reported separately from failures because there are **zero failures**.
- **Smoke scenarios manually validated** (standalone script exercising the real orchestrator, not part of the pytest suite):
  - Normal booking ("Yes, book it")
  - Ambiguous booking ("Yes, but not this week")
  - Opt-out, and opt-out persistence on a later reply
  - Emergency escalation
  - Booking-window enforcement (calendar layer and full booking flow)
  - `TriggerService` gating across multiple simulated days
  - Conflicting signals ("Stop messaging me, I'm bleeding badly")

No manual browser/UI validation was performed as part of this integration.

## 10. Files Added / Major Files Modified

**New files:**
- `agent/policy_guard.py` — the authorization layer
- `core/clock.py` — time abstraction
- `core/trigger_service.py` — actionability filter
- `tests/test_clock.py`, `tests/test_policy_guard.py`, `tests/test_conversation_signals.py`, `tests/test_booking_window.py`, `tests/test_trigger_service.py`, `tests/test_orchestrator_safety.py`

**Major modified files:**
- `agent/orchestrator.py` — PolicyGuard wiring, terminal-state protection, `next_followup_at` carry-forward, booking-window threading (largest diff)
- `agent/conversation.py` — emergency/opt-out/consent signal extraction
- `core/models.py` — `opted_out`, `OPTED_OUT`, `next_followup_at`
- `core/config.py` — `clinic_timezone`, `booking_window_days`
- `core/actions.py` — `RECORD_OPT_OUT`, `REQUEST_CLARIFICATION`
- `core/data_access.py`, `agent/action_handlers.py` — booking-window parameter threading
- `agent/decision.py` — `CLOSED_STATUSES` includes `OPTED_OUT`
- `tests/conftest.py` — new fixtures for the above (additive; existing fixtures untouched)

## 11. How to Test This Branch

```bash
source .venv/bin/activate
python -m pytest tests/ -q
python web/app.py
```

**Manual test matrix** (via the dashboard's simulate-reply endpoint or a Python shell against `FollowUpAgentOrchestrator.handle_incoming_reply`):

| Input | Expected result |
|---|---|
| "Yes, book it" | Booked; confirmation sent |
| "Yes, but not this week" | Clarification requested; not booked |
| "Stop messaging me" | Status → `OPTED_OUT`; opt-out confirmation sent |
| (later) any reply after opt-out, e.g. "yes, book it" | No message sent; status remains `OPTED_OUT` |
| "I'm bleeding badly" | Escalated to staff; no booking attempted |

## 12. Recommended Next Phase

The next product phase is intended to add, in two possible directions **not yet implemented on this branch**:

```
Email → Patient Portal → 7-day slot picker → BOOKED
```
or
```
No suitable slot → PENDING_FUTURE_AVAILABILITY → next_followup_at → future Agent re-trigger
```

**NOT YET IMPLEMENTED** — both require additional design and code beyond what this branch delivers.

## 13. Merge Notes

- This work was developed on `feature/booking-agent`.
- The team should review and merge via a pull request, not by replacing `master` directly.
- Re-run the full test suite after merging, in case `master` has moved since this branch was created.
- If `master` has changed, resolve conflicts **semantically** — re-check that PolicyGuard's authorization step still runs before every side-effecting action after any merge resolution, rather than accepting either side's diff blindly.
