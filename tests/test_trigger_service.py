"""
Tests for core.trigger_service.TriggerService.

PHASE 1 SCOPE: only the basic `next_followup_at` re-trigger capability is
covered here. The fuller park-and-retry lifecycle (PENDING_FUTURE_AVAILABILITY,
pending_reason, contact_attempt_count, last_booking_window) is deferred to a
later phase and is intentionally not exercised by these tests.
"""

from datetime import timedelta

from core.models import CaseStatus
from core.trigger_service import TriggerService

from tests.conftest import make_case, make_fixed_clock, make_patient, FIXED_TODAY


class TestIsActionable:
    def test_overdue_case_with_no_next_followup_at_is_actionable(self):
        service = TriggerService(make_fixed_clock())
        case = make_case(days_overdue=5, status=CaseStatus.PENDING)
        assert service.is_actionable(case) is True

    def test_case_with_zero_days_overdue_and_no_next_followup_at_is_not_actionable(self):
        service = TriggerService(make_fixed_clock())
        case = make_case(days_overdue=0, status=CaseStatus.PENDING)
        assert service.is_actionable(case) is False

    def test_next_followup_at_in_the_future_is_not_yet_actionable(self):
        service = TriggerService(make_fixed_clock())
        case = make_case(
            days_overdue=5,
            status=CaseStatus.PENDING,
            next_followup_at=FIXED_TODAY + timedelta(days=3),
        )
        assert service.is_actionable(case) is False

    def test_next_followup_at_reached_today_is_actionable(self):
        service = TriggerService(make_fixed_clock())
        case = make_case(
            days_overdue=5,
            status=CaseStatus.PENDING,
            next_followup_at=FIXED_TODAY,
        )
        assert service.is_actionable(case) is True

    def test_next_followup_at_in_the_past_is_actionable(self):
        service = TriggerService(make_fixed_clock())
        case = make_case(
            days_overdue=5,
            status=CaseStatus.PENDING,
            next_followup_at=FIXED_TODAY - timedelta(days=1),
        )
        assert service.is_actionable(case) is True

    def test_opted_out_patient_is_never_actionable(self):
        service = TriggerService(make_fixed_clock())
        patient = make_patient(opted_out=True)
        case = make_case(patient=patient, days_overdue=30, status=CaseStatus.PENDING)
        assert service.is_actionable(case) is False

    def test_terminal_statuses_are_never_actionable(self):
        service = TriggerService(make_fixed_clock())
        for status in (CaseStatus.BOOKED, CaseStatus.DECLINED, CaseStatus.ESCALATED, CaseStatus.OPTED_OUT):
            case = make_case(days_overdue=30, status=status)
            assert service.is_actionable(case) is False, f"{status} must not be actionable"


class TestGetActionableCases:
    def test_filters_and_preserves_order(self):
        service = TriggerService(make_fixed_clock())
        actionable = make_case(patient=make_patient("P1"), days_overdue=5, status=CaseStatus.PENDING)
        not_yet = make_case(
            patient=make_patient("P2"),
            days_overdue=5,
            status=CaseStatus.PENDING,
            next_followup_at=FIXED_TODAY + timedelta(days=10),
        )
        closed = make_case(patient=make_patient("P3"), days_overdue=5, status=CaseStatus.BOOKED)

        result = service.get_actionable_cases([actionable, not_yet, closed])
        assert result == [actionable]


class TestCasesDueForRetrigger:
    def test_only_cases_with_reached_next_followup_at_are_returned(self):
        service = TriggerService(make_fixed_clock())
        due = make_case(
            patient=make_patient("P1"),
            days_overdue=5,
            status=CaseStatus.PENDING,
            next_followup_at=FIXED_TODAY,
        )
        not_due = make_case(
            patient=make_patient("P2"),
            days_overdue=5,
            status=CaseStatus.PENDING,
            next_followup_at=FIXED_TODAY + timedelta(days=5),
        )
        never_scheduled = make_case(
            patient=make_patient("P3"), days_overdue=5, status=CaseStatus.PENDING
        )

        result = service.cases_due_for_retrigger([due, not_due, never_scheduled])
        assert result == [due]

    def test_opted_out_case_excluded_even_if_due(self):
        service = TriggerService(make_fixed_clock())
        patient = make_patient(opted_out=True)
        case = make_case(patient=patient, next_followup_at=FIXED_TODAY)
        assert service.cases_due_for_retrigger([case]) == []
