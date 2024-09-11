# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the Open Job Description ActionMessageFilter"""

from __future__ import annotations

import logging
from hashlib import sha256
from openjd.sessions._logging import LoggerAdapter
from logging.handlers import QueueHandler
from queue import SimpleQueue
from typing import Union
from unittest.mock import MagicMock

import pytest

from openjd.sessions._action_filter import (
    ActionMessageKind,
    ActionMonitoringFilter,
)


class TestActionMonitoringFilter:
    @pytest.fixture
    def message_queue(self) -> SimpleQueue:
        return SimpleQueue()

    @pytest.fixture
    def queue_handler(self, message_queue: SimpleQueue) -> QueueHandler:
        return QueueHandler(message_queue)

    def build_logger(
        self, name: str, handler: QueueHandler, filter: ActionMonitoringFilter
    ) -> logging.Logger:
        log = logging.getLogger(".".join((__name__, name)))
        log.setLevel(logging.INFO)
        log.addHandler(handler)
        log.addFilter(filter)
        return log

    @pytest.mark.parametrize(
        "message,kind,value",
        (
            pytest.param(
                "openjd_progress: 50.0",
                ActionMessageKind.PROGRESS,
                float(50),
                id="progress",
            ),
            pytest.param(
                "openjd_status: a status string",
                ActionMessageKind.STATUS,
                "a status string",
                id="status",
            ),
            pytest.param(
                "openjd_fail: an error message",
                ActionMessageKind.FAIL,
                "an error message",
                id="fail",
            ),
            pytest.param(
                "openjd_env: foo=bar",
                ActionMessageKind.ENV,
                {"name": "foo", "value": "bar"},
                id="env",
            ),
            pytest.param(
                "openjd_env: F_F_12=bar",
                ActionMessageKind.ENV,
                {"name": "F_F_12", "value": "bar"},
                id="env, allowable characters",
            ),
            pytest.param(
                "openjd_env: foo=",
                ActionMessageKind.ENV,
                {"name": "foo", "value": ""},
                id="env, assign empty",
            ),
            pytest.param(
                "openjd_env: foo= ",
                ActionMessageKind.ENV,
                {"name": "foo", "value": " "},
                id="env, assign whitespace",
            ),
            pytest.param(
                "openjd_env:  \t foo=bar",
                ActionMessageKind.ENV,
                {"name": "foo", "value": "bar"},
                id="env, leading whitespace",
            ),
            pytest.param(
                "openjd_unset_env: foo",
                ActionMessageKind.UNSET_ENV,
                "foo",
                id="unset_env",
            ),
            pytest.param(
                "openjd_unset_env: F_F_12",
                ActionMessageKind.UNSET_ENV,
                "F_F_12",
                id="unset_env, allowable characters",
            ),
            pytest.param(
                "openjd_unset_env:  \t foo",
                ActionMessageKind.UNSET_ENV,
                "foo",
                id="unset_env, leading whitespace",
            ),
            pytest.param(
                "openjd_session_runtime_loglevel: DEBUG",
                ActionMessageKind.SESSION_RUNTIME_LOGLEVEL,
                logging.DEBUG,
                id="loglevel debug",
            ),
            pytest.param(
                "openjd_session_runtime_loglevel: INFO",
                ActionMessageKind.SESSION_RUNTIME_LOGLEVEL,
                logging.INFO,
                id="loglevel debug",
            ),
            pytest.param(
                "openjd_session_runtime_loglevel: WARNING",
                ActionMessageKind.SESSION_RUNTIME_LOGLEVEL,
                logging.WARNING,
                id="loglevel debug",
            ),
            pytest.param(
                "openjd_session_runtime_loglevel: ERROR",
                ActionMessageKind.SESSION_RUNTIME_LOGLEVEL,
                logging.ERROR,
                id="loglevel debug",
            ),
        ),
    )
    def test_captures_suppress(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        message: str,
        kind: ActionMessageKind,
        value: Union[float, str],
    ) -> None:
        # GIVEN
        h = sha256()
        h.update(message.encode("utf-8"))
        logger_name = "suppress" + h.hexdigest()[0:32]
        callback_mock = MagicMock()
        filter = ActionMonitoringFilter(
            session_id="foo", callback=callback_mock, suppress_filtered=True
        )
        log = self.build_logger(logger_name, queue_handler, filter)
        loga = LoggerAdapter(log, extra={"session_id": "foo"})

        # WHEN
        loga.info(message)

        # THEN
        callback_mock.assert_called_once_with(kind, value, False)
        assert message_queue.qsize() == 0, "Message is suppressed"

    def test_ignores_different_session(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # GIVEN
        message = "openjd_fail: an error message"
        h = sha256()
        h.update(message.encode("utf-8"))
        logger_name = "suppress" + h.hexdigest()[0:32]
        callback_mock = MagicMock()
        filter = ActionMonitoringFilter(
            session_id="foo", callback=callback_mock, suppress_filtered=True
        )
        log = self.build_logger(logger_name, queue_handler, filter)

        # WHEN
        log.info(message)

        # THEN
        callback_mock.assert_not_called()
        assert message_queue.qsize() == 1

    @pytest.mark.parametrize(
        "message,kind,value",
        (
            pytest.param(
                "openjd_progress: 50.0",
                ActionMessageKind.PROGRESS,
                float(50),
                id="progress",
            ),
            pytest.param(
                "openjd_status: a status string",
                ActionMessageKind.STATUS,
                "a status string",
                id="status",
            ),
            pytest.param(
                "openjd_fail: an error message",
                ActionMessageKind.FAIL,
                "an error message",
                id="fail",
            ),
            pytest.param(
                "openjd_env: foo=bar",
                ActionMessageKind.ENV,
                {"name": "foo", "value": "bar"},
                id="env",
            ),
            pytest.param(
                "openjd_unset_env: foo",
                ActionMessageKind.UNSET_ENV,
                "foo",
                id="unset_env",
            ),
        ),
    )
    def test_captures_no_suppress(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        message: str,
        kind: ActionMessageKind,
        value: Union[float, str],
    ) -> None:
        # GIVEN
        h = sha256()
        h.update(message.encode("utf-8"))
        logger_name = "no_suppress" + h.hexdigest()[0:32]
        callback_mock = MagicMock()
        filter = ActionMonitoringFilter(session_id="foo", callback=callback_mock)
        log = self.build_logger(logger_name, queue_handler, filter)
        loga = LoggerAdapter(log, extra={"session_id": "foo"})

        # WHEN
        loga.info(message)

        # THEN
        callback_mock.assert_called_once_with(kind, value, False)
        assert message_queue.qsize() == 1, "Message passed through"
        assert message_queue.get(block=False).getMessage() == message

    @pytest.mark.parametrize(
        "message",
        (
            pytest.param("openjd_progress:50.0", id="progress, no space"),
            pytest.param("OPENJD_PROGRESS: 50.0", id="progress, uppercase"),
            pytest.param(" openjd_progress: 50.0", id="progress, leading whitespace"),
            pytest.param(
                "openjd_status:a status string",
                id="status, no space",
            ),
            pytest.param(
                "OPENJD_STATUS: a status string",
                id="status, uppercase",
            ),
            pytest.param(
                " openjd_status: a status string",
                id="status, leading whitespace",
            ),
            pytest.param(
                "openjd_fail:an error message",
                id="fail, no space",
            ),
            pytest.param(
                "OPENJD_FAIL: an error message",
                id="fail, uppercase",
            ),
        ),
    )
    def test_malformed_does_not_match_no_callback(
        self, queue_handler: QueueHandler, message: str
    ) -> None:
        # GIVEN
        h = sha256()
        h.update(message.encode("utf-8"))
        logger_name = "malformed" + h.hexdigest()[0:32]
        callback_mock = MagicMock()
        filter = ActionMonitoringFilter(session_id="foo", callback=callback_mock)
        log = self.build_logger(logger_name, queue_handler, filter)
        loga = LoggerAdapter(log, extra={"session_id": "foo"})

        # WHEN
        loga.info(message)

        # THEN
        callback_mock.assert_not_called()

    @pytest.mark.parametrize(
        "message",
        (
            pytest.param(
                "openjd_env: foo",
                id="env, missing assignment",
            ),
            pytest.param(
                "openjd_env: foo =value",
                id="env, extra whitespace",
            ),
            pytest.param(
                "openjd_env: 1F_F_12=bar",
                id="env, start with digit",
            ),
            pytest.param(
                "openjd_env: F😁=bar",
                id="env, non-latin",
            ),
        ),
    )
    def test_malformed_set_env_assigment(self, queue_handler: QueueHandler, message: str) -> None:
        # GIVEN
        h = sha256()
        h.update(message.encode("utf-8"))
        logger_name = "malformed" + h.hexdigest()[0:32]
        callback_mock = MagicMock()
        filter = ActionMonitoringFilter(session_id="foo", callback=callback_mock)
        log = self.build_logger(logger_name, queue_handler, filter)
        loga = LoggerAdapter(log, extra={"session_id": "foo"})

        # WHEN
        loga.info(message)

        # THEN
        err_message = "Failed to parse environment variable assignment."
        callback_mock.assert_called_once_with(ActionMessageKind.ENV, err_message, True)

    @pytest.mark.parametrize(
        "message",
        (
            pytest.param(
                "openjd_env:foo=bar",
                id="env, no space",
            ),
            pytest.param(
                "OPENJD_ENV: foo=bar",
                id="env, uppercase",
            ),
            pytest.param(
                " openjd_env: foo=bar",
                id="env, leading whitespace",
            ),
            pytest.param(
                "openjd_unset_env:foo",
                id="unset_env, no space",
            ),
            pytest.param(
                "OPENJD_UNSET_ENV: foo",
                id="unset_env, uppercase",
            ),
            pytest.param(
                " openjd_unset_env: foo",
                id="unset_env, leading whitespace",
            ),
        ),
    )
    def test_malformed_openjd_regex(self, queue_handler: QueueHandler, message: str) -> None:
        # GIVEN
        h = sha256()
        h.update(message.encode("utf-8"))
        logger_name = "malformed" + h.hexdigest()[0:32]
        callback_mock = MagicMock()
        filter = ActionMonitoringFilter(session_id="foo", callback=callback_mock)
        log = self.build_logger(logger_name, queue_handler, filter)
        loga = LoggerAdapter(log, extra={"session_id": "foo"})

        # WHEN
        loga.info(message)

        # THEN
        err_message = f"Open Job Description: Incorrectly formatted openjd env command ({message})"
        callback_mock.assert_called_once_with(ActionMessageKind.FAIL, err_message, True)

    @pytest.mark.parametrize(
        "message",
        (
            pytest.param(
                "openjd_unset_env: foo=bar",
                id="unset_env, bad value",
            ),
            pytest.param(
                "openjd_unset_env: 1F_F_12",
                id="unset_env, start with digit",
            ),
            pytest.param(
                "openjd_unset_env: F😁",
                id="unset_env, non-latin",
            ),
        ),
    )
    def test_malformed_does_not_match_unset_env(
        self, queue_handler: QueueHandler, message: str
    ) -> None:
        # GIVEN
        h = sha256()
        h.update(message.encode("utf-8"))
        logger_name = "malformed" + h.hexdigest()[0:32]
        callback_mock = MagicMock()
        filter = ActionMonitoringFilter(session_id="foo", callback=callback_mock)
        log = self.build_logger(logger_name, queue_handler, filter)
        loga = LoggerAdapter(log, extra={"session_id": "foo"})

        # WHEN
        loga.info(message)

        # THEN
        err_message = "Failed to parse environment variable name."
        callback_mock.assert_called_once_with(ActionMessageKind.UNSET_ENV, err_message, True)

    @pytest.mark.parametrize(
        "message",
        (
            pytest.param("openjd_progress: fifty", id="not a float"),
            pytest.param("openjd_progress: -0.01", id="too small"),
            pytest.param("openjd_progress: 100.1", id="too big"),
        ),
    )
    def test_progress_appends_error(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler, message: str
    ) -> None:
        # When the floating point value in an openjd_progress message is either
        # not a float or out of the allowable range of values, we always pass the
        # message through to the log and we append an error message to it.
        #
        # GIVEN
        h = sha256()
        h.update(message.encode("utf-8"))
        logger_name = "appends" + h.hexdigest()[0:32]
        callback_mock = MagicMock()
        filter = ActionMonitoringFilter(session_id="foo", callback=callback_mock)
        log = self.build_logger(logger_name, queue_handler, filter)
        loga = LoggerAdapter(log, extra={"session_id": "foo"})
        expected_message = (
            message
            + " -- ERROR: Progress must be a floating point value between 0.0 and 100.0, inclusive."
        )

        # WHEN
        loga.info(message)

        # THEN
        callback_mock.assert_not_called()
        assert message_queue.qsize() == 1, "Message passed through"
        assert message_queue.get(block=False).getMessage() == expected_message

    def test_handles_non_string(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # GIVEN
        h = sha256()
        h.update("exception-test".encode("utf-8"))
        logger_name = "non_string" + h.hexdigest()[0:32]
        callback_mock = MagicMock()
        filter = ActionMonitoringFilter(
            session_id="foo", callback=callback_mock, suppress_filtered=True
        )
        log = self.build_logger(logger_name, queue_handler, filter)
        loga = LoggerAdapter(log, extra={"session_id": "foo"})

        # WHEN
        try:
            raise Exception("Surprise!")
        except Exception as e:
            loga.exception(e)

        # THEN
        callback_mock.assert_not_called()
        assert message_queue.qsize() == 1
        assert "Exception: Surprise!" in message_queue.get(block=False).getMessage()
