"""Keep secrets and personal data out of logs.

`RedactingFilter` is attached to every handler. It masks JWTs, bearer tokens, cookie and
token headers, password fields and email addresses wherever they appear in a log line,
including in formatted arguments and exception text.
"""

import logging
import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # JWTs: three base64url segments, the first starting with '{"' encoded (eyJ).
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*", re.ASCII), "[JWT]"),
    (re.compile(r"(?i)\bbearer\s+[^\s'\",]+", re.ASCII), "Bearer [REDACTED]"),
    (
        re.compile(
            r"(?i)\b(authorization|cookie|set-cookie|restricted_refresh_token)"
            r"(['\"]?\s*[:=]\s*['\"]?)[^'\"\n]+",
            re.ASCII,
        ),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(r"(?i)(\"?password\"?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)", re.ASCII),
        r"\1[REDACTED]",
    ),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.ASCII), "[EMAIL]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Rewrites each record's final message so nothing sensitive reaches a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info:
            # Format the traceback now so it is redacted too, then drop the raw exception.
            formatter = logging.Formatter()
            message = f"{message}\n{formatter.formatException(record.exc_info)}"
            record.exc_info = None
            record.exc_text = None
        record.msg = redact(message)
        record.args = None
        return True


def install_redaction(logger: logging.Logger | None = None) -> None:
    """Attach the filter to every handler on `logger` (root by default). Idempotent."""
    target = logger if logger is not None else logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
