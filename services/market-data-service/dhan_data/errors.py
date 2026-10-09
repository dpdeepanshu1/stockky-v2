"""dhan_data/errors.py - exception types the callers branch on (group 270)."""
from __future__ import annotations


class DhanError(Exception):
    """Base class. Never carries a token or header value in its text."""


class DhanNotConfigured(DhanError):
    """No usable credentials (nothing stored, key missing/mismatched, token expired)."""


class DhanAuthError(DhanError):
    """401/403 or a Dhan auth error code: token invalid or expired."""


class DhanSubscriptionError(DhanError):
    """The Data API subscription is missing/inactive for this account."""


class DhanRateLimitError(DhanError):
    """429 or Dhan's too-many-requests code."""


class DhanNoDataError(DhanError):
    """Answered, but nothing usable for this instrument/range."""


class DhanApiError(DhanError):
    """Any other failure reported by Dhan or the network."""
