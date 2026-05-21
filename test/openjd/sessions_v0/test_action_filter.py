# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the Open Job Description ActionMessageFilter"""

from __future__ import annotations

import logging
from logging.handlers import QueueHandler
from queue import SimpleQueue
from typing import Union
from unittest.mock import MagicMock

import pytest

from openjd.sessions._action_filter import (
    ActionMessageKind,
    ActionMonitoringFilter,
)
from .conftest import setup_action_filter_test


class TestActionMonitoringFilter:
    @pytest.fixture
    def message_queue(self) -> SimpleQueue:
        return SimpleQueue()

    @pytest.fixture
    def queue_handler(self, message_queue: SimpleQueue) -> QueueHandler:
        return QueueHandler(message_queue)

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
                id="loglevel info",
            ),
            pytest.param(
                "openjd_session_runtime_loglevel: WARNING",
                ActionMessageKind.SESSION_RUNTIME_LOGLEVEL,
                logging.WARNING,
                id="loglevel warning",
            ),
            pytest.param(
                "openjd_session_runtime_loglevel: ERROR",
                ActionMessageKind.SESSION_RUNTIME_LOGLEVEL,
                logging.ERROR,
                id="loglevel error",
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
        loga, _, callback_mock = setup_action_filter_test(
            queue_handler=queue_handler,
            suppress_filtered=True,
        )

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
        callback_mock = MagicMock()
        filter = ActionMonitoringFilter(
            session_id="foo", callback=callback_mock, suppress_filtered=True
        )
        log = logging.getLogger("test.different_session")
        log.setLevel(logging.INFO)
        log.addHandler(queue_handler)
        log.addFilter(filter)

        # WHEN - Note we don't use LoggerAdapter with session_id here
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
        loga, _, callback_mock = setup_action_filter_test(queue_handler=queue_handler)

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
        loga, _, callback_mock = setup_action_filter_test(queue_handler=queue_handler)

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
            pytest.param(
                "openjd_redacted_env: foo",
                id="redacted_env, missing assignment",
            ),
            pytest.param(
                "openjd_redacted_env: foo =value",
                id="redacted_env, extra whitespace",
            ),
            pytest.param(
                "openjd_redacted_env: 1F_F_12=bar",
                id="redacted_env, start with digit",
            ),
            pytest.param(
                "openjd_redacted_env: F😁=bar",
                id="redacted_env, non-latin",
            ),
        ),
    )
    def test_malformed_set_env_assigment(self, queue_handler: QueueHandler, message: str) -> None:
        # GIVEN
        loga, _, callback_mock = setup_action_filter_test(queue_handler=queue_handler)

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
                "openjd_redacted_env:foo=bar",
                id="redacted_env, no space",
            ),
            pytest.param(
                "OPENJD_REDACTED_ENV: foo=bar",
                id="redacted_env, uppercase",
            ),
            pytest.param(
                " openjd_redacted_env: foo=bar",
                id="redacted_env, leading whitespace",
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
        loga, _, callback_mock = setup_action_filter_test(queue_handler=queue_handler)

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
        loga, _, callback_mock = setup_action_filter_test(queue_handler=queue_handler)

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
        loga, _, callback_mock = setup_action_filter_test(queue_handler=queue_handler)
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
        loga, _, callback_mock = setup_action_filter_test(
            queue_handler=queue_handler,
            suppress_filtered=True,
        )

        # WHEN
        try:
            raise Exception("Surprise!")
        except Exception as e:
            loga.exception(e)

        # THEN
        callback_mock.assert_not_called()
        assert message_queue.qsize() == 1
        assert "Exception: Surprise!" in message_queue.get(block=False).getMessage()

    def test_redacted_env_redacts_value(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        """Test that openjd_redacted_env properly redacts values in logs."""
        # GIVEN
        message = "openjd_redacted_env: PASSWORD=secret123"
        loga, _, callback_mock = setup_action_filter_test(
            queue_handler=queue_handler,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # WHEN
        loga.info(message)

        # THEN
        # Check that the callback was called with the correct parameters
        callback_mock.assert_called_once_with(
            ActionMessageKind.ENV, {"name": "PASSWORD", "value": "secret123"}, False
        )

        # Check that the message in the log is redacted
        assert message_queue.qsize() == 1
        log_message = message_queue.get(block=False).getMessage()
        assert "openjd_redacted_env: PASSWORD=********" in log_message
        assert "secret123" not in log_message

    def test_redacted_env_with_warning(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler, monkeypatch
    ) -> None:
        """Test that redacted_env messages log a warning when the extension is not enabled."""
        # GIVEN
        mock_log = MagicMock()
        monkeypatch.setattr("openjd.sessions._action_filter.LOG", mock_log)

        message = "openjd_redacted_env: SECRET_VAR=secret_value"
        loga, _, callback_mock = setup_action_filter_test(
            queue_handler=queue_handler,
            enabled_extensions=[],  # No extensions enabled
        )

        # WHEN
        loga.info(message)

        # THEN
        mock_log.warning.assert_called_once()
        assert "REDACTED_ENV_VARS extension is not enabled" in mock_log.warning.call_args[0][0]

        # The callback should NOT be called since the extension is not enabled
        callback_mock.assert_not_called()

        # Check that the message in the log is redacted
        assert message_queue.qsize() == 1, "Message passed through"
        log_message = message_queue.get(block=False).getMessage()
        assert "SECRET_VAR=********" in log_message
        assert "secret_value" not in log_message

    def test_redacted_env_uses_fixed_length_redaction(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        """Test that openjd_redacted_env uses a fixed-length redaction regardless of value length."""
        # GIVEN
        # Create a single logger setup for both tests
        loga, _, callback_mock = setup_action_filter_test(queue_handler=queue_handler)

        short_message = "openjd_redacted_env: KEY=x"
        long_message = "openjd_redacted_env: TOKEN=abcdefghijklmnopqrstuvwxyz1234567890"
        expected_redacted_format = "********"

        # WHEN
        loga.info(short_message)
        loga.info(long_message)

        # THEN
        # Check that both messages use the same fixed-length redaction
        assert message_queue.qsize() == 2, "Both messages passed through"

        log_message1 = message_queue.get(block=False).getMessage()
        assert log_message1 == f"openjd_redacted_env: KEY={expected_redacted_format}"

        log_message2 = message_queue.get(block=False).getMessage()
        assert log_message2 == f"openjd_redacted_env: TOKEN={expected_redacted_format}"

    def test_redacted_env_redacts_subsequent_occurrences(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        """Test that values from openjd_redacted_env are redacted in all subsequent log messages."""
        # GIVEN
        loga, _, callback_mock = setup_action_filter_test(
            queue_handler=queue_handler,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # WHEN
        # First log the redacted_env message to set up the value
        redacted_message = "openjd_redacted_env: PASSWORD=supersecret123"
        loga.info(redacted_message)

        # Then log a regular message containing the sensitive value
        regular_message = "Here is the password: supersecret123 for your reference"
        loga.info(regular_message)

        # THEN
        # Check that both messages were logged
        assert message_queue.qsize() == 2, "Both messages should be in the queue"

        # First message should have redacted the value in the openjd_redacted_env line
        first_log = message_queue.get(block=False).getMessage()
        assert first_log == "openjd_redacted_env: PASSWORD=********"
        assert "supersecret123" not in first_log

        # Second message should have redacted the sensitive value
        second_log = message_queue.get(block=False).getMessage()
        assert "supersecret123" not in second_log
        assert "Here is the password: ********" in second_log

        # The callback should have been called with the actual value for env processing
        callback_mock.assert_any_call(
            ActionMessageKind.ENV, {"name": "PASSWORD", "value": "supersecret123"}, False
        )

    def test_redacted_env_handles_multiple_values(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        """Test that multiple redacted values are all properly redacted in logs."""
        # GIVEN
        loga, _, callback_mock = setup_action_filter_test(
            queue_handler=queue_handler,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # WHEN
        # Set up multiple redacted values
        loga.info("openjd_redacted_env: PASSWORD=password123")
        loga.info("openjd_redacted_env: API_KEY=abcdef123456")

        # Log a message containing both sensitive values
        loga.info("Using PASSWORD=password123 and API_KEY=abcdef123456 for authentication")

        # THEN
        # Skip the first two messages which are the redacted_env declarations
        message_queue.get(block=False)
        message_queue.get(block=False)

        # Check that the third message has both values redacted
        final_message = message_queue.get(block=False).getMessage()
        assert "password123" not in final_message
        assert "abcdef123456" not in final_message
        assert "Using PASSWORD=******** and API_KEY=******** for authentication" in final_message

    def test_redacted_env_with_extension(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        """Test that redacted_env messages set environment variables when the extension is enabled."""
        # GIVEN
        message = "openjd_redacted_env: PASSWORD=secret123"
        loga, action_filter, callback_mock = setup_action_filter_test(
            queue_handler=queue_handler,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # WHEN
        loga.info(message)

        # THEN
        # The callback should be called with the environment variable info
        callback_mock.assert_called_once_with(
            ActionMessageKind.ENV, {"name": "PASSWORD", "value": "secret123"}, False
        )

        # The message should be redacted in the logs
        assert message_queue.qsize() == 1
        log_message = message_queue.get(block=False).getMessage()
        assert "openjd_redacted_env: PASSWORD=********" in log_message
        assert "secret123" not in log_message

    def test_malformed_redacted_env_commands(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        """Test handling of malformed redacted_env commands with spaces or missing equals sign."""
        # GIVEN
        loga, _, callback_mock = setup_action_filter_test(
            queue_handler=queue_handler,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # Case 1: Space after key (key =value)
        message1 = "openjd_redacted_env: PASSWORD =secret123"

        # Case 2: Missing equals sign (keyvalue)
        message2 = "openjd_redacted_env: SECRETsensitivedata"

        # WHEN
        loga.info(message1)
        loga.info(message2)

        # THEN
        # Check that both messages were processed
        assert message_queue.qsize() == 2

        # For Case 1 (key =value), we should still try to redact the value
        log_message1 = message_queue.get(block=False).getMessage()
        assert "openjd_redacted_env: PASSWORD =********" in log_message1
        assert "secret123" not in log_message1

        # For Case 2 (missing equals), we should redact the entire content after the prefix
        log_message2 = message_queue.get(block=False).getMessage()
        assert "openjd_redacted_env: ********" in log_message2
        assert "SECRETsensitivedata" not in log_message2

        # Neither case should set an environment variable
        callback_mock.assert_not_called()
