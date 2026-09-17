"""Custom exceptions needing a structured JSON body beyond plain {"detail": ...}."""

from __future__ import annotations


class RateLimitExceeded(Exception):
    """A 429 that also carries a machine-readable `retry_after_seconds`, so callers
    can drive an accurate countdown without parsing `detail`'s English sentence."""

    def __init__(self, detail: str, retry_after_seconds: int):
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds
