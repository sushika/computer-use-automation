"""Redaction chokepoint. SPEC §7: the prompt is a leak path, not only write-out."""

from __future__ import annotations

import re
from typing import Final

# SSN with dashes: 123-45-6789
_SSN_DASHED: Final[re.Pattern[str]] = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Bare 9-digit SSN. 6-digit member ids (100241) are left intact.
_SSN_PLAIN: Final[re.Pattern[str]] = re.compile(r"\b\d{9}\b")
# Account-like digit runs. 8–17 digits, no separators.
# Deliberate scope choice: balances stay visible. "$12,847.55" contains
# commas and a decimal, so it does not match this pattern. The discovery
# goal asks the model to read a savings balance; masking it would make
# requirement 3.1 ungradeable. We are not masking currency.
_ACCOUNT: Final[re.Pattern[str]] = re.compile(r"\b\d{8,17}\b")

_SSN_REPLACEMENT: Final[str] = "[SSN]"
_ACCOUNT_REPLACEMENT: Final[str] = "[ACCOUNT]"


def redact_text(text: str) -> str:
    """Mask SSN-like and account-number patterns. Idempotent enough to run twice."""
    masked: str = _SSN_DASHED.sub(_SSN_REPLACEMENT, text)
    masked = _SSN_PLAIN.sub(_SSN_REPLACEMENT, masked)
    masked = _ACCOUNT.sub(_ACCOUNT_REPLACEMENT, masked)
    return masked
