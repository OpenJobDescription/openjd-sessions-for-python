# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
from __future__ import annotations

from typing import Any, MutableMapping, TypedDict
import logging

from .._logging import LogContent

__all__ = ["LOG", "LoggerAdapter", "LogExtraInfo", "LogContent"]

# LogContent is the public openjd.sessions.LogContent class, not a second Flag enum
# with the same members. Consumers route records on the LOG logger by type-checking
# `record.openjd_log_content` against openjd.sessions.LogContent, and enum.Flag
# comparisons are class-scoped, so a duplicate class would fail that check.


class LogExtraInfo(TypedDict):
    """
    A TypedDict which contains extra information to be added to the "extra" key of a log record.
    """

    openjd_log_content: LogContent | None


class LoggerAdapter(logging.LoggerAdapter):
    """
    LoggerAdapter which merges the "extra" kwarg instead of replacing with what the LoggerAdapter was initialized with.
    """

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        """
        Typically the LoggerAdaptor simply replaces the `extra` key in the kwargs with the one initialized with the
        adapter. However, we want to merge the two dictionaries, so we override it here.
        """
        if "extra" not in kwargs:
            kwargs["extra"] = self.extra
        else:
            kwargs["extra"] |= self.extra
        return msg, kwargs


# The logger for the sessions module. Name is hard-coded to "openjd.sessions"
# so that it matches the name used by the Rust-backed session runtime in
# openjd-rs (which uses `log::...!(target: "openjd.sessions", ...)` via the
# session_log! macro). Handlers attached to this logger therefore receive both
# Python-side and Rust-side log records.
#
# Note: this file lives inside the v1 subpackage (openjd.sessions._v1._logging),
# but the logger it exports is intentionally the v0/v1-shared "openjd.sessions",
# not "openjd.sessions._v1". Python logging propagation flows child→parent, not
# parent→child, so attaching handlers at "openjd.sessions._v1" would miss
# records emitted from Rust to "openjd.sessions".
LOG = logging.getLogger("openjd.sessions")
"""
The logger of the openjd sessions module. The logger has the name openjd.sessions and is used 
throughout the openjd sessions module to provide information on actions the module is taking, as
well as any output from commands run during Sessions.

Some LogRecords sent to the logger will have an extra attribute named "openjd_log_content" who's 
value is a LogContent which provides information on what data is contained in the LogRecord, or None
if there is no applicable LogContent field (for example, a message like "Ending Session")

If the LogRecord does not have the "openjd_log_content" no guarantees are made as to what content
is in the LogRecord. LogRecords that contain LogContent.EXECPTION_INFO may also transitively include
potentially sensitive information like filepaths or host info due to the nature of exception messages.

LogRecords emitted by the Rust-backed session runtime additionally carry a "session_id" (str) and
an "openjd_timestamp_usec" (int). pyo3-log delivers all structured fields to Python as strings, so
importing openjd.sessions._v1 installs a filter on this logger that converts "openjd_log_content"
back to a LogContent and "openjd_timestamp_usec" back to an int before any handler sees the record.
"""
LOG.setLevel(logging.INFO)


class _RustLogRecordNormalizingFilter(logging.Filter):
    """Restores the Python types of the structured fields on Rust-emitted records.

    The Rust session runtime tags each record it emits with ``session_id`` (str),
    ``openjd_log_content`` (a LogContent bitmask as an unsigned integer) and
    ``openjd_timestamp_usec`` (unsigned integer). pyo3-log renders every value
    with its Rust ``Display`` and passes the strings as ``extra`` to
    ``Logger.makeRecord``, so ``openjd_log_content`` arrives as e.g. "8" and the
    timestamp as a decimal string; pyo3-log has no hook to keep the types.

    Installed on the logger, not on a handler: pyo3-log delivers records with
    ``Logger.handle()``, which runs the logger's filters before any handler (and
    any handler-level filter) sees the record. Values this LogContent cannot
    represent (e.g. a bit added by a newer runtime) are left as they arrived.
    The filter never drops a record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Rust formats these unsigned integers as plain decimal digits. Only
        # convert that form: int() would also accept signs and whitespace, and
        # a negative value would become a LogContent complement rather than an
        # error.
        content = getattr(record, "openjd_log_content", None)
        if isinstance(content, str) and content.isdigit():
            try:
                setattr(record, "openjd_log_content", LogContent(int(content)))
            except ValueError:
                pass
        timestamp = getattr(record, "openjd_timestamp_usec", None)
        if isinstance(timestamp, str) and timestamp.isdigit():
            setattr(record, "openjd_timestamp_usec", int(timestamp))
        return True


def _install_rust_log_record_normalizing_filter(logger: logging.Logger) -> None:
    """Install one _RustLogRecordNormalizingFilter on ``logger``; idempotent."""
    if not any(isinstance(f, _RustLogRecordNormalizingFilter) for f in logger.filters):
        logger.addFilter(_RustLogRecordNormalizingFilter())


# openjd.sessions._v1 imports this module before the modules that load the Rust
# extension, so the filter is in place before the runtime emits its first record.
_install_rust_log_record_normalizing_filter(LOG)

_banner_log_extra = LogExtraInfo(openjd_log_content=LogContent.BANNER)


def log_section_banner(logger: LoggerAdapter, section_title: str) -> None:
    logger.info("")
    logger.info("==============================================", extra=_banner_log_extra)
    logger.info(f"--------- {section_title}", extra=_banner_log_extra)
    logger.info("==============================================", extra=_banner_log_extra)


def log_subsection_banner(logger: LoggerAdapter, section_title: str) -> None:
    logger.info("----------------------------------------------", extra=_banner_log_extra)
    logger.info(section_title, extra=_banner_log_extra)
    logger.info("----------------------------------------------", extra=_banner_log_extra)
