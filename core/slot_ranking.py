"""
Slot ranking for the Patient Portal's "no-show-aware" slot ordering.

Context: when a patient has a recent CANCELLED/EXPIRED appointment on file
(the closest proxy this system has for "missed appointment" - see
SchedulingDatabaseCalendarAdapter.get_last_missed_slot_hint and
SchedulingDatabase.get_last_non_active_appointment_for_patient for why this
is a heuristic, not a true no-show flag), the portal should show slots near
that appointment's session/time first, WITHOUT restricting the patient to
only that time - every other available slot in the window must remain
visible and selectable, just ordered after the preferred ones.

This module is intentionally pure/stateless: it takes an already-fetched
list of SlotOption (the real 7-day-from-today window, unrelated to the
missed appointment's date) and an optional "hint" slot, and returns a
grouped, ranked view. It invents no appointment times - every slot it
ranks came from the real availability list passed in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from core.scheduling_calendar_adapter import SlotOption

# Ordinal position of each session within a day, used only to measure
# "closeness" to the missed appointment's session for ranking purposes -
# not a clinic configuration value and not used for validation.
_SESSION_ORDER = {"morning": 0, "afternoon": 1, "evening": 2}


def _session_distance(session_a: str, session_b: str) -> int:
    """
    Rough same-day closeness between two session names, for ranking only.
    Unknown session names (any clinic config we don't special-case) sort
    as neutrally distant rather than crashing.
    """
    if session_a == session_b:
        return 0
    order_a = _SESSION_ORDER.get(session_a)
    order_b = _SESSION_ORDER.get(session_b)
    if order_a is None or order_b is None:
        return 1
    return abs(order_a - order_b)


@dataclass
class RankedSlots:
    """
    Result of ranking a slot list against an optional missed-appointment
    hint.

    Attributes:
        preferred: Slots that share the hint's session (e.g. all
            "afternoon" slots when the missed appointment was in the
            afternoon), ordered soonest-first. Empty when there is no hint.
        other: Every remaining available slot (all slots, when there is no
            hint), ordered soonest-first. Never empty just because
            `preferred` is non-empty - the caller must always be able to
            fall back to a different time.
        used_hint: Whether a missed-appointment hint actually influenced
            this ranking (False when hint was None, or when it matched no
            session ranking-wise - `other` is then simply the full list).
    """
    preferred: list[SlotOption]
    other: list[SlotOption]
    used_hint: bool


def rank_slots_by_missed_appointment(
    available: list[SlotOption],
    missed_hint: Optional[SlotOption],
) -> RankedSlots:
    """
    Split/order `available` into "preferred" (near the missed
    appointment's session) and "other" (everything else), both
    soonest-first.

    The search window itself (which dates are even in `available`) is
    entirely the caller's responsibility and must already be anchored to
    the current clinic date - this function only re-orders/labels slots
    that are already in the window; it never changes which dates are
    considered, and never invents a slot that is not in `available`.

    Args:
        available: The real, already-fetched available slots for the
            current booking window (from
            SchedulingDatabaseCalendarAdapter.find_available_slot_options).
        missed_hint: The patient's most recent missed-appointment proxy
            slot (from get_last_missed_slot_hint), or None if the patient
            has no such history - in which case no ranking preference is
            applied and every slot is returned as `other`, unchanged in
            order.

    Returns:
        RankedSlots with `preferred` + `other` together containing every
        slot from `available` exactly once.
    """
    ordered = sorted(available, key=lambda s: s.datetime_utc)

    if missed_hint is None:
        return RankedSlots(preferred=[], other=ordered, used_hint=False)

    preferred: list[SlotOption] = []
    other: list[SlotOption] = []
    for slot in ordered:
        if _session_distance(slot.session, missed_hint.session) == 0:
            preferred.append(slot)
        else:
            other.append(slot)

    if not preferred:
        # Nothing shares the missed appointment's session in this window
        # (e.g. the clinic no longer offers that session) - fall back to
        # the plain, unranked list rather than reporting a hint that had
        # no actual effect.
        return RankedSlots(preferred=[], other=ordered, used_hint=False)

    return RankedSlots(preferred=preferred, other=other, used_hint=True)
