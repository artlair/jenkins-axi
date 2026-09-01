"""Presentation helpers: time deltas for display.

Duration and progress math (durations/percentages derived from build data)
lives in api.py beside the data it derives from; this module only owns
absolute-time deltas, which nothing else derives from.
"""

from __future__ import annotations


def when(timestamp_ms: int | None, now_ms: int) -> str:
    """Compact relative time: 'just now', '14m ago', '3h ago', '5d ago'.
    'never' states the zero for absent builds (AXI §5)."""
    if not timestamp_ms:
        return "never"
    delta_s = max(0, int((now_ms - timestamp_ms) / 1000))
    if delta_s < 45:
        return "just now"
    if delta_s < 90 * 60:
        return f"{delta_s // 60}m ago"
    if delta_s < 36 * 3600:
        return f"{delta_s // 3600}h ago"
    return f"{delta_s // 86400}d ago"
