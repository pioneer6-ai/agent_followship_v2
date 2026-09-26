# Deployment evidence 4 - Automated test suite

Captured: 2026-09-25T09:47:34+00:00

> **Snapshot scope.** The result below (`811 passed`) is the suite as it stood at
> commit `fbcc6c1` only. It is **not** the current count on `master` (`f35639a`).
> Re-run `proposal/scripts/capture_tests_evidence.py` to re-capture against a
> later revision.

## What this proves

The revision identified in evidence 3 passes its full automated test
suite on the recorded interpreter. The suite is the executable form of
the behaviour contracts: tool schemas, error taxonomy, provider
selection, agent loop control flow, delivery gating and the web API.
The verbatim pytest output is stored alongside this file.

## Result

- Status: **PASS** (exit code 0)
- Command: `/Users/martinchen/agent_followship_v3/.venv/bin/python -m pytest tests -q` (local virtualenv that ran the capture; CPython 3.14.5)
- Passed: 811
- Failed: 0
- Errors: 0
- Skipped: 0
- Warnings: 16
- Duration: 14.43s

```
811 passed, 16 warnings in 14.43s
```

## Environment

- Python: 3.14.5
- Platform: macOS-27.0-arm64-arm-64bit-Mach-O
- Raw output: `proposal/evidence/artifacts/pytest_output.txt`

## Coverage by module

| Test module | Tests | Lines |
| --- | ---: | ---: |
| `tests/test_agent_delivery.py` | 50 | 1,093 |
| `tests/test_agent_loop.py` | 39 | 643 |
| `tests/test_auth_calendar.py` | 20 | 549 |
| `tests/test_aws_tools.py` | 73 | 727 |
| `tests/test_baseline_urgency.py` | 6 | 172 |
| `tests/test_booking_protection.py` | 13 | 643 |
| `tests/test_booking_window.py` | 7 | 102 |
| `tests/test_calendar_auth.py` | 15 | 358 |
| `tests/test_calendar_date_shift.py` | 7 | 388 |
| `tests/test_calendar_migrations.py` | 5 | 94 |
| `tests/test_calendar_unification.py` | 18 | 527 |
| `tests/test_clock.py` | 7 | 53 |
| `tests/test_conversation_signals.py` | 16 | 190 |
| `tests/test_docs_consistency.py` | 20 | 520 |
| `tests/test_error_taxonomy.py` | 14 | 131 |
| `tests/test_hospital_setup.py` | 64 | 746 |
| `tests/test_llm_providers.py` | 79 | 718 |
| `tests/test_orchestrator_safety.py` | 22 | 535 |
| `tests/test_patient_portal.py` | 31 | 621 |
| `tests/test_patient_portal_loading_regression.py` | 5 | 243 |
| `tests/test_patient_portal_slot_times.py` | 16 | 598 |
| `tests/test_policy_guard.py` | 15 | 179 |
| `tests/test_providers.py` | 48 | 576 |
| `tests/test_staff_management.py` | 10 | 428 |
| `tests/test_tool_contract.py` | 29 | 391 |
| `tests/test_trigger_service.py` | 10 | 111 |
| `tests/test_urgency_config.py` | 9 | 73 |
| `tests/test_urgency_scorer.py` | 7 | 139 |
| `tests/test_web_upload.py` | 26 | 367 |
| **Total** | **681** | **11,915** |

The inventory counts test *functions* (681);
pytest reports 811 passing *cases* because several are
parameterised and expand at collection time. Both numbers describe the
same suite.

## Known limitation found while capturing this evidence

The suite exercises the real orchestrator and therefore its real audit
logger, which appends to `audit_log.json` in the working directory. In
other words, running the tests mutates the audit record. Evidence 1 is
computed from a frozen snapshot for exactly this reason, and isolating
the audit path during tests is a prerequisite for production sign-off.
