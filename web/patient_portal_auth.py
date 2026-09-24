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
