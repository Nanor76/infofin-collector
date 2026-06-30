from __future__ import annotations

import logging
import re
from typing import Any

class RedactingFilter(logging.Filter):
    def __init__(self, token: str | None = None) -> None:
        super().__init__()
        self.token = token
        # Regex patterns to redact sensitive query parameters in URLs
        self.patterns = [
            re.compile(r"(api_token)=([^&\s\"'\)]+)", re.IGNORECASE),
            re.compile(r"(token)=([^&\s\"'\)]+)", re.IGNORECASE),
            re.compile(r"(apikey)=([^&\s\"'\)]+)", re.IGNORECASE),
        ]

    def redact(self, text: str) -> str:
        if not text:
            return text
        # Redact exact occurrences of the token
        if self.token:
            text = text.replace(self.token, "[REDACTED_TOKEN]")
        # Redact key=value patterns where key is api_token, token, or apikey
        for pattern in self.patterns:
            text = pattern.sub(r"\1=[REDACTED_TOKEN]", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        # Pre-format message with args if present to avoid format specifier issues
        if record.args:
            try:
                record.msg = str(record.msg) % record.args
                record.args = ()
            except Exception:
                # Fallback to redacting args individually if merging fails
                if isinstance(record.args, tuple):
                    record.args = tuple(
                        self.redact(str(arg)) if isinstance(arg, str) else arg
                        for arg in record.args
                    )
                elif isinstance(record.args, dict):
                    record.args = {
                        k: (self.redact(v) if isinstance(v, str) else v)
                        for k, v in record.args.items()
                    }

        # Redact the log message
        if isinstance(record.msg, str):
            record.msg = self.redact(record.msg)

        # Redact exceptions and their traceback representations
        if record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            if exc_value:
                orig_msg = str(exc_value)
                cleaned_msg = self.redact(orig_msg)
                if cleaned_msg != orig_msg:
                    try:
                        # Construct a new exception of the same type with the scrubbed message
                        new_exc = exc_type(cleaned_msg)
                        new_exc.__traceback__ = exc_tb
                        record.exc_info = (exc_type, new_exc, exc_tb)
                    except Exception:
                        pass

        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)

        return True

def setup_logging_redactor(token: str | None = None) -> None:
    redactor = RedactingFilter(token)
    
    # Target all possible loggers that might output EODHD URLs/tokens
    loggers_to_redact = [
        logging.getLogger(),  # Root logger
        logging.getLogger("urllib3"),
        logging.getLogger("urllib3.connectionpool"),
        logging.getLogger("requests"),
        logging.getLogger("infofin"),
        logging.getLogger("infofin.screener"),
        logging.getLogger("infofin.screener.eodhd"),
        logging.getLogger("infofin.screener.cli"),
    ]
    
    for logger in loggers_to_redact:
        # Clear existing RedactingFilters to allow token update in tests
        logger.filters = [f for f in logger.filters if not isinstance(f, RedactingFilter)]
        logger.addFilter(redactor)

    # Attach to all existing handlers on the root logger
    for handler in logging.getLogger().handlers:
        handler.filters = [f for f in handler.filters if not isinstance(f, RedactingFilter)]
        handler.addFilter(redactor)
