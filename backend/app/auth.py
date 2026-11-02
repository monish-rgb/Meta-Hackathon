"""Run-token auth for FlowGate (backend/app/auth.py)."""

import os
import secrets
from typing import Optional

from app.store import get_run_by_token

DEFAULT_ADMIN_KEY = "flowgate-dev-admin"
DEMO_ADMIN_KEY = DEFAULT_ADMIN_KEY

_TRUTHY = {"1", "true", "yes", "on"}


def resolve_demo_mode(demo_mode: Optional[bool] = None) -> bool:
    """Demo mode is explicit: arg > FLOWGATE_DEMO_MODE env > off."""
    if demo_mode is not None:
        return bool(demo_mode)
    return os.environ.get("FLOWGATE_DEMO_MODE", "").strip().lower() in _TRUTHY


def create_token() -> str:
    return secrets.token_urlsafe(32)


def extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1]:
        return parts[1]
    return None


def authenticate(conn, authorization: Optional[str]) -> Optional[dict]:
    token = extract_bearer(authorization)
    if not token:
        return None
    return get_run_by_token(conn, token)


def resolve_admin_key(
    admin_key: Optional[str] = None, demo_mode: Optional[bool] = None
) -> Optional[str]:
    """Resolve the admin key, fail-closed.

    Order: explicit param > FLOWGATE_ADMIN_KEY > GATEWAY_ADMIN_KEY >
    the well-known dev key *only* when demo mode is explicitly enabled.
    With no configured key and no demo mode, returns None (admin disabled).
    """
    if admin_key is not None:
        return str(admin_key)
    flowgate_key = os.environ.get("FLOWGATE_ADMIN_KEY")
    if flowgate_key:
        return flowgate_key
    gateway_key = os.environ.get("GATEWAY_ADMIN_KEY")
    if gateway_key:
        return gateway_key
    if resolve_demo_mode(demo_mode):
        return DEFAULT_ADMIN_KEY
    return None


def _matches(candidate: Optional[str], expected: Optional[str]) -> bool:
    if candidate is None or expected is None:
        return False
    return secrets.compare_digest(str(candidate), str(expected))


def is_admin_key(candidate: Optional[str], admin_key: Optional[str] = None) -> bool:
    """True when ``candidate`` is the admin key (raw or ``Bearer <key>``)."""
    expected = str(admin_key) if admin_key is not None else resolve_admin_key()
    if _matches(candidate, expected):
        return True
    bearer = extract_bearer(candidate) if candidate else None
    return _matches(bearer, expected)


def verify_admin_key(candidate: Optional[str], admin_key: Optional[str] = None) -> bool:
    return is_admin_key(candidate, admin_key)


def check_admin_key(candidate: Optional[str], admin_key: Optional[str] = None) -> bool:
    return is_admin_key(candidate, admin_key)


def is_admin(
    authorization: Optional[str] = None,
    x_admin_key: Optional[str] = None,
    admin_key: Optional[str] = None,
) -> bool:
    """True when the request carries a valid admin credential.

    Accepts ``X-Admin-Key: <key>`` or ``Authorization: Bearer <key>``.
    Comparison uses :func:`secrets.compare_digest`.
    """
    expected = str(admin_key) if admin_key is not None else resolve_admin_key()
    if _matches(x_admin_key, expected):
        return True
    bearer = extract_bearer(authorization) if authorization else None
    return _matches(bearer, expected)


def admin_authorized(
    authorization: Optional[str] = None,
    x_admin_key: Optional[str] = None,
    admin_key: Optional[str] = None,
) -> bool:
    return is_admin(authorization, x_admin_key, admin_key)


def check_admin(
    authorization: Optional[str] = None,
    x_admin_key: Optional[str] = None,
    admin_key: Optional[str] = None,
) -> bool:
    return is_admin(authorization, x_admin_key, admin_key)


def require_admin(
    authorization: Optional[str] = None,
    x_admin_key: Optional[str] = None,
    admin_key: Optional[str] = None,
) -> bool:
    return is_admin(authorization, x_admin_key, admin_key)
