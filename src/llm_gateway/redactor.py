"""Streaming PII redactor: emails, SSNs, and credit-card numbers replaced
with `[REDACTED]`, including matches that straddle chunk boundaries —
without ever buffering the full response.

Algorithm: keep a small bounded "holdback" tail of the most recently fed
text un-emitted, since a match could still be forming there. Each `feed()`
call re-runs the redaction regex over `<held-back tail> + <new chunk>` (a
small, bounded string — never the whole stream so far) and emits everything
except the last `HOLDBACK` characters, which stay buffered for the next
call. `flush()` releases that final tail once the source stream ends.

Correctness depends on one assumption: a match is only guaranteed to be
caught if it completes within `HOLDBACK` characters of where it started, so
`HOLDBACK` must be chosen at least as large as the longest pattern we match
(SSN 11 chars, credit card ~19 chars with separators, realistic emails
comfortably under 64). That's a deliberate, documented trade-off, not an
oversight: a larger `HOLDBACK` is safer against very long split matches but
directly costs latency, since nothing can be emitted — no matter how benign
— until more than `HOLDBACK` characters have arrived. 64 is the smallest
value that comfortably covers all three target pattern types without
holding back an unreasonable amount of ordinary text.
"""

from __future__ import annotations

import re

_EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
_SSN = r"\b\d{3}-\d{2}-\d{4}\b"
_CREDIT_CARD = r"\b(?:\d{4}[- ]?){3}\d{4}\b"

PII_PATTERN = re.compile(f"(?:{_EMAIL})|(?:{_SSN})|(?:{_CREDIT_CARD})")

REDACTED = "[REDACTED]"


class PiiStreamRedactor:
    HOLDBACK = 64

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> str:
        """Feed newly-arrived text. Returns the portion now safe to emit
        (already redacted); may be empty if not enough has accumulated yet
        to be sure no in-progress match is still forming at the tail."""
        self._buffer = PII_PATTERN.sub(REDACTED, self._buffer + chunk)
        if len(self._buffer) <= self.HOLDBACK:
            return ""
        split = self._safe_split_point(len(self._buffer) - self.HOLDBACK)
        emit, self._buffer = self._buffer[:split], self._buffer[split:]
        return emit

    def _safe_split_point(self, naive_split: int) -> int:
        """Pull the split point back if it would land inside a `[REDACTED]`
        marker we just inserted — cosmetic (the fully concatenated stream is
        already correct either way), but a marker torn across two emissions
        looks like a bug to any consumer that doesn't reassemble first."""
        marker_len = len(REDACTED)
        window_start = max(0, naive_split - marker_len + 1)
        idx = self._buffer.find(REDACTED, window_start, naive_split + marker_len)
        if idx != -1 and idx < naive_split < idx + marker_len:
            return idx
        return naive_split

    def flush(self) -> str:
        """Call once the source stream has ended: returns (and clears) the
        final held-back tail, already redacted."""
        remaining = self._buffer
        self._buffer = ""
        return remaining
