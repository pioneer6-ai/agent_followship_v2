"""
Demo access-token resolution for the patient self-service portal.

DEMO ONLY - NOT PRODUCTION AUTHENTICATION.

This module maps an opaque access token to a patient/case. It exists so the
portal route (``/patient/<access_token>``) never has to expose or accept a
raw ``patient_id`` from the browser: the token is the only thing the URL and
the client-side JavaScript ever see, and the backend is the only place that
resolves it to a real patient.

Why this is demo-only, and what production requires instead:
  - Tokens here are generated once, in-memory, and never expire. A real
    deployment must issue signed, expiring tokens (e.g. a JWT with a short
    TTL, or a one-time-use link rotated on each portal visit) so a leaked
    URL cannot be replayed indefinitely.
  - There is no rate limiting, no revocation, and no audit trail specific
    to token usage beyond what the orchestrator's own audit log already
    records for the case.
  - Token -> patient_id mapping lives in a process-local dict, exactly like
    the rest of this project's in-memory data store (MockPatientDataStore).
    It does not survive a restart and is not safe for multiple app
    instances.

Design choices that DO carry over to production, and are kept even in this
demo:
  - The token itself is an opaque, unguessable random string (not the
    patient_id, not a predictable sequence) - see `generate_token`.
  - Resolution is one-way and server-side only: given a token, the backend
    looks up the patient; the reverse (deriving a token from a patient_id)
    is never exposed to the client.
  - Every route that accepts a token must call `resolve_token` and treat a
    ``None`` result as "not found" (do not distinguish "invalid token" from
    "unknown patient" in the response, so a token cannot be used to probe
    for valid patient IDs).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Optional


@dataclass
class PatientAccessTokenStore:
    """
    In-memory token -> patient_id mapping.

    One instance is created per Flask app (see web/app.py), sharing the
    same lifetime as the app's other demo singletons (data_store, calendar).
    """

    _tokens: dict[str, str]

    def __init__(self) -> None:
        self._tokens = {}

    def issue_token(self, patient_id: str) -> str:
        """
        Create (or reuse) a demo access token for a patient.

        Args:
            patient_id: The patient this token grants portal access to.

        Returns:
            An opaque token string. Safe to place in a URL; does not reveal
            `patient_id`.
        """
        for token, existing_patient_id in self._tokens.items():
            if existing_patient_id == patient_id:
                return token
        token = generate_token()
        self._tokens[token] = patient_id
        return token

    def resolve_token(self, token: str) -> Optional[str]:
        """
        Resolve a token to the patient_id it grants access to.

        Args:
            token: The opaque token from the URL.

        Returns:
            The patient_id, or None if the token is unknown. Callers must
            not treat "unknown token" differently from "unknown patient" in
            any response sent back to the browser.
        """
        return self._tokens.get(token)

    def revoke_token(self, token: str) -> None:
        """Invalidate a token immediately (e.g. after a booking completes,
        if the demo wants single-use links - not enforced by default)."""
        self._tokens.pop(token, None)


def generate_token(num_bytes: int = 24) -> str:
    """
    Generate an unguessable, URL-safe opaque token.

    Args:
        num_bytes: Amount of randomness backing the token. 24 bytes (192
            bits) is far more than needed for a demo but costs nothing.

    Returns:
        A URL-safe token string containing no patient-identifying data.
    """
    return secrets.token_urlsafe(num_bytes)


def get_patient_portal_path(token_store: PatientAccessTokenStore, patient_id: str) -> str:
    """
    The SINGLE shared implementation of "turn a patient_id into their
    Patient Portal link". Both the staff-facing
    ``POST /api/patients/<id>/portal-link`` route (web/app.py) and the
    follow-up email composer (agent/notifications.py, via
    ``build_portal_link_provider`` below) call this exact function -
    neither one has its own token-issuing logic, and the email layer does
    NOT call the Flask route over HTTP to get here; it imports and calls
    this plain Python function directly, same as the route does.

    Args:
        token_store: The app's one PatientAccessTokenStore instance.
        patient_id: The patient to link to. ``issue_token`` returns the
            SAME token every time for the same patient_id (see its own
            docstring), so calling this repeatedly for one patient (e.g.
            once per reminder email, or once from the staff dashboard) is
            idempotent and always yields the same link.

    Returns:
        A path of the form ``/patient/<access_token>`` - relative, with no
        scheme/host. Callers that need an absolute URL (e.g. an email,
        where a relative link is meaningless) must prepend a base URL
        themselves - see ``build_portal_link_provider``.
    """
    token = token_store.issue_token(patient_id)
    return f"/patient/{token}"


def build_portal_link_provider(token_store: PatientAccessTokenStore, base_url: str = ""):
    """
    Build a zero-argument-per-call callable that turns a patient_id into a
    full, absolute Patient Portal URL - the shape
    ``agent.notifications.MessageComposerAgent`` needs so it can embed a
    patient-specific link in a reminder email without importing Flask,
    ``web.app``, or making any HTTP call. This is what keeps the email
    layer decoupled from the web layer while still reusing the exact same
    token logic as the staff-facing portal-link route.

    Args:
        token_store: The app's one PatientAccessTokenStore instance -
            pass the SAME instance the Flask routes use (see web/app.py)
            so a link minted for an email and a link minted via the
            dashboard for the same patient are identical.
        base_url: Scheme+host to prepend, e.g. ``"https://clinic.example.com"``
            (no trailing slash). Left empty, the returned URLs stay
            relative (``/patient/<token>``) - fine for tests, but not a
            usable link in a real email, so production wiring (web/app.py)
            must supply a real base_url.

    Returns:
        A ``Callable[[str], str]`` mapping patient_id -> full portal URL.
    """
    normalized_base = base_url.rstrip("/")

    def provider(patient_id: str) -> str:
        path = get_patient_portal_path(token_store, patient_id)
        return f"{normalized_base}{path}" if normalized_base else path

    return provider
