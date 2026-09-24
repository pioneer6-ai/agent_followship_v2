"""
TriggerService - decides WHICH cases are actionable right now.

This is the "Trigger" layer of the reasoning loop (Trigger -> Context ->
Reason/Decide -> Safety Authorization -> Tool Execution -> Observe Result ->
State Update): a small, pure, deterministic filter over a list of
FollowUpCase objects, given a Clock. It is NOT a decision engine - it does
not decide what to do with an actionable case (that remains
agent/decision.py's job), and it does not itself call any tool, send
anything, or mutate case state. Its only responsibility is: "given all known
cases and the current date, which ones deserve the agent's attention today?"

This separation matters for the intended swap-in path: today this is called
from FollowUpAgentOrchestrator.run_daily_cycle. Later, this exact same class
can be invoked by a real scheduler (e.g. AWS EventBridge + Lambda on a cron
schedule) with zero changes to the decision/delivery/tools layers - only the
caller of `get_actionable_cases` changes.

PHASE 1 SCOPE: this implements only the basic `next_followup_at` re-trigger
capability. The fuller park-and-retry lifecycle (PENDING_FUTURE_AVAILABILITY,
pending_reason, contact_attempt_count, last_booking_window) is explicitly
deferred to a later phase and is NOT implemented here.

A case is actionable when:
    - It is not in a terminal-for-outreach state (BOOKED, DECLINED,
      ESCALATED, OPTED_OUT) - those cases require no further automatic
      outreach.
    - AND either:
        (a) `case.next_followup_at` is set and `clock.today() >=
            next_followup_at` (the explicit re-trigger case), or
        (b) `case.next_followup_at` is None and `case.days_overdue > 0`
            (the plain "still overdue, never explicitly scheduled" case -
            reuses the same days_overdue semantics RecallRuleEngine already
            computes, so a case freshly produced by the daily cycle is
            immediately actionable without needing next_followup_at to be
            set first).
"""

from __future__ import annotations

from core.clock import Clock, SystemClock
from core.models import FollowUpCase, CaseStatus


# Case statuses that mean "no further automatic outreach should happen",
# regardless of days_overdue or next_followup_at. These are the terminal
# states for the OUTREACH lifecycle (not necessarily terminal for the
# clinical relationship - e.g. DECLINED could still be revisited manually
# by staff, but never automatically re-triggered).
_NOT_ACTIONABLE_STATUSES = frozenset({
    CaseStatus.BOOKED,
    CaseStatus.DECLINED,
    CaseStatus.ESCALATED,
    CaseStatus.OPTED_OUT,
})


class TriggerService:
    """
    Deterministic, Clock-driven filter for "which cases need agent
    attention today". See module docstring for the exact actionability
    rule.
    """

    def __init__(self, clock: Clock | None = None):
        self.clock = clock or SystemClock()

    def is_actionable(self, case: FollowUpCase) -> bool:
        """True if this single case should be handed to the agent today."""
        if case.patient.opted_out:
            return False
        if case.status in _NOT_ACTIONABLE_STATUSES:
            return False

        today = self.clock.today()
        if case.next_followup_at is not None:
            return today >= case.next_followup_at

        return case.days_overdue > 0

    def get_actionable_cases(self, cases: list[FollowUpCase]) -> list[FollowUpCase]:
        """
        Filter a list of cases down to the ones actionable today.

        Args:
            cases: All known cases (any status/history).

        Returns:
            The subset that should be handed to the agent for this
            trigger cycle, in the same relative order as given (callers
            that want urgency-based ordering should sort separately, e.g.
            with agent.business_rules.UrgencyScorer.sort_by_urgency - this
            method deliberately does not re-implement that).
        """
        return [c for c in cases if self.is_actionable(c)]

    def cases_due_for_retrigger(self, cases: list[FollowUpCase]) -> list[FollowUpCase]:
        """
        Narrower view: only cases with an explicit `next_followup_at` that
        has now been reached, excluding plain newly-overdue cases that never
        had a next_followup_at set. Useful for tests/demos to assert on the
        re-trigger behavior in isolation from the ordinary daily-overdue
        trigger.
        """
        today = self.clock.today()
        return [
            c for c in cases
            if c.next_followup_at is not None
            and today >= c.next_followup_at
            and not c.patient.opted_out
            and c.status not in _NOT_ACTIONABLE_STATUSES
        ]
