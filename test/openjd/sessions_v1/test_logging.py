# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the openjd.sessions._v1 logging module.

The subject under test is the filter that ``openjd.sessions._v1._logging``
installs on the "openjd.sessions" logger. pyo3-log delivers the Rust session
runtime's structured log fields to Python as strings, so ``openjd_log_content``
arrives as a decimal bitmask string such as "8" and ``openjd_timestamp_usec`` as
a decimal string; the filter restores the documented Python types.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Iterator

import pytest

import openjd.sessions
import openjd.sessions._v1
from openjd.sessions import LogContent
from openjd.sessions._v1._logging import (
    LOG,
    _RustLogRecordNormalizingFilter,
    _install_rust_log_record_normalizing_filter,
)


def _record(**extra: Any) -> logging.LogRecord:
    return logging.makeLogRecord(
        {
            "name": LOG.name,
            "msg": "message",
            "levelno": logging.INFO,
            "levelname": "INFO",
            **extra,
        }
    )


class _CollectingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def log_filter() -> _RustLogRecordNormalizingFilter:
    return _RustLogRecordNormalizingFilter()


class TestNormalizingFilterLogContent:
    @pytest.mark.parametrize(
        "value, expected",
        [
            *[(str(member.value), member) for member in LogContent],
            ("10", LogContent.FILE_PATH | LogContent.COMMAND_OUTPUT),
            ("0", LogContent(0)),
        ],
    )
    def test_decimal_string_becomes_log_content(
        self, log_filter: _RustLogRecordNormalizingFilter, value: str, expected: LogContent
    ) -> None:
        # GIVEN
        record = _record(openjd_log_content=value)

        # WHEN
        result = log_filter.filter(record)

        # THEN
        assert result is True
        assert record.openjd_log_content is expected  # type: ignore[attr-defined]
        assert type(record.openjd_log_content) is openjd.sessions.LogContent  # type: ignore[attr-defined]

    def test_already_typed_value_is_left_alone(
        self, log_filter: _RustLogRecordNormalizingFilter
    ) -> None:
        # GIVEN
        record = _record(openjd_log_content=LogContent.BANNER)

        # WHEN
        log_filter.filter(record)

        # THEN
        assert record.openjd_log_content is LogContent.BANNER  # type: ignore[attr-defined]

    def test_missing_attribute_is_not_added(
        self, log_filter: _RustLogRecordNormalizingFilter
    ) -> None:
        # GIVEN
        record = _record()

        # WHEN
        log_filter.filter(record)

        # THEN
        assert not hasattr(record, "openjd_log_content")
        assert not hasattr(record, "openjd_timestamp_usec")

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "abc",
            "1.5",
            "-1",
            # A bit this LogContent does not define, e.g. from a newer runtime.
            "4096",
            None,
            8,
        ],
    )
    def test_unconvertible_value_is_preserved_unchanged(
        self, log_filter: _RustLogRecordNormalizingFilter, value: Any
    ) -> None:
        # GIVEN
        record = _record(openjd_log_content=value)

        # WHEN
        result = log_filter.filter(record)

        # THEN
        assert result is True
        assert record.openjd_log_content == value  # type: ignore[attr-defined]
        assert type(record.openjd_log_content) is type(value)  # type: ignore[attr-defined]


class TestNormalizingFilterTimestamp:
    def test_digit_string_becomes_int(self, log_filter: _RustLogRecordNormalizingFilter) -> None:
        # GIVEN
        record = _record(openjd_timestamp_usec="1762288000123456")

        # WHEN
        log_filter.filter(record)

        # THEN
        assert record.openjd_timestamp_usec == 1762288000123456  # type: ignore[attr-defined]
        assert type(record.openjd_timestamp_usec) is int  # type: ignore[attr-defined]

    @pytest.mark.parametrize("value", [123, "", "soon", "1.5", None])
    def test_unconvertible_value_is_preserved_unchanged(
        self, log_filter: _RustLogRecordNormalizingFilter, value: Any
    ) -> None:
        # GIVEN
        record = _record(openjd_timestamp_usec=value)

        # WHEN
        log_filter.filter(record)

        # THEN
        assert record.openjd_timestamp_usec == value  # type: ignore[attr-defined]
        assert type(record.openjd_timestamp_usec) is type(value)  # type: ignore[attr-defined]

    def test_session_id_is_not_touched(self, log_filter: _RustLogRecordNormalizingFilter) -> None:
        # GIVEN
        record = _record(session_id="123", openjd_log_content="8")

        # WHEN
        log_filter.filter(record)

        # THEN
        assert record.session_id == "123"  # type: ignore[attr-defined]


class TestFilterInstallation:
    def test_importing_v1_installs_exactly_one_normalizer_on_the_logger(self) -> None:
        # GIVEN openjd.sessions._v1 is imported (module import above)
        installed = [
            f
            for f in logging.getLogger("openjd.sessions").filters
            if isinstance(f, _RustLogRecordNormalizingFilter)
        ]

        # THEN
        assert len(installed) == 1

    def test_repeated_install_does_not_stack(self) -> None:
        # GIVEN
        logger = logging.getLogger(f"openjd.sessions.test.{uuid.uuid4().hex}")

        # WHEN
        _install_rust_log_record_normalizing_filter(logger)
        _install_rust_log_record_normalizing_filter(logger)

        # THEN
        assert len(logger.filters) == 1
        assert isinstance(logger.filters[0], _RustLogRecordNormalizingFilter)


class TestInstalledFilterOnTheLoggerHandlePath:
    """pyo3-log hands records to ``Logger.handle()``, which applies the logger's
    filters before any handler. Handlers, and filters on handlers, must see the
    normalized record."""

    @pytest.fixture
    def handler(self) -> Iterator[_CollectingHandler]:
        handler = _CollectingHandler()
        LOG.addHandler(handler)
        try:
            yield handler
        finally:
            LOG.removeHandler(handler)

    def test_handler_sees_normalized_record(self, handler: _CollectingHandler) -> None:
        # GIVEN
        marker = uuid.uuid4().hex
        record = _record(
            msg=marker, openjd_log_content="32", openjd_timestamp_usec="7", session_id="s"
        )

        # WHEN
        LOG.handle(record)

        # THEN
        (seen,) = [r for r in handler.records if r.getMessage() == marker]
        assert seen.openjd_log_content is LogContent.PROCESS_CONTROL  # type: ignore[attr-defined]
        assert seen.openjd_timestamp_usec == 7  # type: ignore[attr-defined]
        assert seen.session_id == "s"  # type: ignore[attr-defined]

    def test_handler_level_filter_sees_normalized_record(self, handler: _CollectingHandler) -> None:
        # GIVEN
        marker = uuid.uuid4().hex
        seen_types: list[type] = []

        class _TypeRecordingFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                if record.getMessage() == marker:
                    seen_types.append(type(getattr(record, "openjd_log_content", None)))
                return True

        handler.addFilter(_TypeRecordingFilter())

        # WHEN
        LOG.handle(_record(msg=marker, openjd_log_content="128"))

        # THEN
        assert seen_types == [LogContent]

    def test_untagged_record_still_reaches_the_handler(self, handler: _CollectingHandler) -> None:
        # GIVEN
        marker = uuid.uuid4().hex

        # WHEN
        LOG.handle(_record(msg=marker))

        # THEN
        assert [r.getMessage() for r in handler.records if r.getMessage() == marker] == [marker]


class TestLogContentIsTheOnePublicClass:
    def test_v1_log_content_is_the_public_class(self) -> None:
        assert openjd.sessions._v1.LogContent is openjd.sessions.LogContent

    def test_members_and_bit_values_are_unchanged(self) -> None:
        assert {m.name: m.value for m in LogContent} == {
            "BANNER": 1,
            "FILE_PATH": 2,
            "FILE_CONTENTS": 4,
            "COMMAND_OUTPUT": 8,
            "EXCEPTION_INFO": 16,
            "PROCESS_CONTROL": 32,
            "PARAMETER_INFO": 64,
            "HOST_INFO": 128,
        }
