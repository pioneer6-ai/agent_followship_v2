"""
Shared fixtures for the messaging tool-layer tests.

Everything here is offline: no network, no SMTP, no ``anthropic`` package. The
transport and the SMTP connection are injected doubles, while the tools,
providers, error classification and the tool-use loop under test are the real
production code paths.
"""

from __future__ import annotations
import os
import sys
from typing import Any, Dict

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.config import MessagingConfig  # noqa: E402
from tools.messaging import EscalationLog, build_tool_registry  # noqa: E402
from tools.transport import FakeSmtpConnection, FakeTransport  # noqa: E402

#: Fake credentials: enough for every provider to consider itself configured,
#: so the real request-building and error-classification paths execute.
#: ``MESSAGING_DRY_RUN=0`` opts into live sending, which is what the provider
#: tests exercise; the safety default is covered separately.
CONFIGURED_ENV: Dict[str, str] = {
    "MESSAGING_DRY_RUN": "0",
    "META_WHATSAPP_ACCESS_TOKEN": "test-token",
    "META_WHATSAPP_PHONE_NUMBER_ID": "100000000000001",
    "TWILIO_ACCOUNT_SID": "ACtesttesttesttesttesttesttesttest",
    "TWILIO_AUTH_TOKEN": "test-auth-token",
    "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886",
    "TWILIO_SMS_FROM": "+14155238886",
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USERNAME": "frontdesk@brightsmile.example",
    "SMTP_PASSWORD": "test-smtp-password",
    "EMAIL_FROM": "frontdesk@brightsmile.example",
}

PHONE = "+15550001111"
EMAIL = "patient@example.com"

#: The two verified test recipients the AWS tools are allowed to reach.
AWS_TEST_PHONE = "+6583536885"
AWS_TEST_EMAIL = "martinchenonly1@gmail.com"

#: AWS configuration for the allow-listed test recipients. ``AWS_REGION`` and
#: the allow-lists are pinned so the AWS tools behave identically regardless of
#: the developer's own ``.env``/``~/.aws/config``.
AWS_ENV: Dict[str, str] = {
    "AWS_REGION": "ap-southeast-1",
    "AWS_SES_SOURCE": AWS_TEST_EMAIL,
    "AWS_SMS_ALLOWED_NUMBERS": AWS_TEST_PHONE,
    "AWS_EMAIL_ALLOWED_ADDRESSES": AWS_TEST_EMAIL,
}


@pytest.fixture
def env() -> Dict[str, str]:
    """Credentials for all three channels."""
    return dict(CONFIGURED_ENV)


@pytest.fixture
def config(env: Dict[str, str]) -> MessagingConfig:
    """A fully configured :class:`MessagingConfig`."""
    return MessagingConfig.from_env(env)


@pytest.fixture
def escalation_log() -> EscalationLog:
    """An in-memory escalation sink."""
    return EscalationLog()


def make_registry(
    env: Dict[str, str],
    *,
    transport: FakeTransport = None,
    smtp: FakeSmtpConnection = None,
    escalation_log: EscalationLog = None,
    aws_clients: Dict[str, Any] = None,
) -> Any:
    """
    Build a registry wired to offline doubles.

    Args:
        env: Environment mapping (use ``{}`` for an unconfigured channel set).
        transport: Scripted HTTP responses.
        smtp: Scripted SMTP connection.
        escalation_log: Escalation sink to inspect afterwards.
        aws_clients: ``{"sms": client, "email": client}`` fake boto3 clients for
            the AWS tools; omitted means the real boto3 client would be built,
            which no test should reach.

    Returns:
        The configured :class:`~tools.messaging.ToolRegistry`.
    """
    return build_tool_registry(
        MessagingConfig.from_env(env),
        transport=transport if transport is not None else FakeTransport(),
        smtp_connection_factory=(lambda: smtp) if smtp is not None else None,
        escalation_log=escalation_log or EscalationLog(),
        aws_clients=aws_clients,
    )


@pytest.fixture
def registry(env: Dict[str, str]) -> Any:
    """Registry with all channels configured and a scripted transport."""
    return make_registry(env)


# ---------------------------------------------------------------------------
# Fixtures for Phase 1 safety-layer tests (clock, opt-out, PolicyGuard,
# booking window, TriggerService). These build core/agent domain objects
# directly, independent of the messaging tool-layer fixtures above.
# ---------------------------------------------------------------------------

from datetime import date as _date, datetime as _datetime, timedelta as _timedelta
from zoneinfo import ZoneInfo as _ZoneInfo

from core.clock import FixedClock
from core.models import CaseStatus, ContactChannel, FollowUpCase, PatientRecord, UrgencyLevel

FIXED_TODAY = _date(2026, 9, 24)
CLINIC_TZ = _ZoneInfo("Asia/Singapore")
FIXED_NOW = _datetime(2026, 9, 24, 10, 0, tzinfo=CLINIC_TZ)


def make_fixed_clock(now: _datetime = FIXED_NOW) -> FixedClock:
    """Build a deterministic, injectable clock for tests."""
    return FixedClock(fixed_now=now)


def make_patient(
    patient_id="P100",
    name="Test Patient",
    treatment_type="cleaning",
    recall_interval_days=180,
    days_since_visit=200,
    no_show_history=0,
    preferred_channel=ContactChannel.SMS,
    opted_out=False,
):
    """Build a PatientRecord with sensible defaults for tests."""
    return PatientRecord(
        patient_id=patient_id,
        name=name,
        contact_info={
            ContactChannel.SMS: "+1-555-0000",
            ContactChannel.EMAIL: "test@example.com",
            ContactChannel.WHATSAPP: "+1-555-0000",
            ContactChannel.PHONE_CALL: "+1-555-0000",
        },
        preferred_channel=preferred_channel,
        last_visit_date=FIXED_TODAY - _timedelta(days=days_since_visit),
        treatment_type=treatment_type,
        recall_interval_days=recall_interval_days,
        no_show_history=no_show_history,
        opted_out=opted_out,
    )


def make_case(
    patient=None,
    days_overdue=20,
    urgency=UrgencyLevel.LOW,
    status=CaseStatus.MESSAGE_SENT,
    reminder_count=1,
    next_followup_at=None,
):
    """Build a FollowUpCase with sensible defaults for tests."""
    if patient is None:
        patient = make_patient()
    return FollowUpCase(
        patient=patient,
        days_overdue=days_overdue,
        urgency=urgency,
        reason="test case",
        status=status,
        reminder_count=reminder_count,
        next_followup_at=next_followup_at,
    )


@pytest.fixture
def policy_guard():
    """A fresh PolicyGuard instance."""
    from agent.policy_guard import PolicyGuard

    return PolicyGuard()


@pytest.fixture
def conversation_manager():
    """A fresh ConversationManager instance."""
    from agent.conversation import ConversationManager

    return ConversationManager()
