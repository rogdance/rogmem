"""Shared schedule constants for the release probe.

The polling interval under a healthy state, the growth of the backoff
window on repeated failures, and the longer idle window when the source
URL is missing or malformed are all centralized here so the scheduler
and the UI hints can keep in sync.
"""

from __future__ import annotations

PROBE_INTERVAL = 15 * 60
REFRESH_COOLDOWN = 60
BACKOFF_MIN_SECONDS = 5 * 60
BACKOFF_MAX_SECONDS = 30 * 60
CONFIG_PROBE_INTERVAL = 30 * 60
