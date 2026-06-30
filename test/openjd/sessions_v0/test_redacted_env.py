# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for redacted environment variables functionality."""

import logging
import logging.handlers
import pytest
from queue import SimpleQueue
from unittest.mock import MagicMock

from openjd.sessions._action_filter import ActionMessageKind
from .conftest import setup_action_filter_test


class TestRedactedEnv:
    """Tests for redacted environment variables functionality."""

    # Basic functionality tests
    def test_basic_redacted_env(
        self, message_queue: SimpleQueue, queue_handler: logging.handlers.QueueHandler
    ) -> None:
        """Test basic functionality of redacted environment variables."""
        # Setup
        callback_mock = MagicMock()
        loga, _, _ = setup_action_filter_test(
            queue_handler=queue_handler,
            callback=callback_mock,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # WHEN
        loga.info("openjd_redacted_env: KEY=VALUE")

        # THEN
        # Check that the callback was called with the correct parameters
        callback_mock.assert_called_once_with(
            ActionMessageKind.ENV, {"name": "KEY", "value": "VALUE"}, False
        )

        # Check that the message in the log is redacted
        assert message_queue.qsize() == 1
        log_record = message_queue.get(block=False).getMessage()
        assert "VALUE" not in log_record
        assert "********" in log_record

    # Edge cases tests using parameterization
    @pytest.mark.parametrize(
        "case,expected_key,expected_value,should_set_env",
        [
            # Space after equals
            ("openjd_redacted_env: KEY= VALUE", "KEY", " VALUE", True),
            # Space before equals
            ("openjd_redacted_env: KEY =VALUE", None, "VALUE", False),
            # No equals
            ("openjd_redacted_env: KEYVALUE", None, "KEYVALUE", False),
            # Multiple equals
            ("openjd_redacted_env: KEY=VALUE=MORE", "KEY", "VALUE=MORE", True),
            # Empty value
            ("openjd_redacted_env: KEY=", "KEY", "", True),
        ],
    )
    def test_redacted_env_edge_cases(
        self,
        message_queue: SimpleQueue,
        queue_handler: logging.handlers.QueueHandler,
        case: str,
        expected_key: str,
        expected_value: str,
        should_set_env: bool,
    ) -> None:
        """Test edge cases for redacted environment variables."""
        # Setup
        callback_mock = MagicMock()
        loga, _, _ = setup_action_filter_test(
            queue_handler=queue_handler,
            callback=callback_mock,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # WHEN
        loga.info(case)

        # THEN
        if should_set_env:
            # Check that the callback was called with the correct parameters
            callback_mock.assert_called_once_with(
                ActionMessageKind.ENV, {"name": expected_key, "value": expected_value}, False
            )
        else:
            # Check that the callback was NOT called (no env var should be set)
            callback_mock.assert_not_called()

        # Check that the message in the log is redacted
        assert message_queue.qsize() == 1
        log_message = message_queue.get(block=False).getMessage()
        if expected_value:  # Skip empty string check
            assert expected_value not in log_message

        # Check that subsequent logs with the value are redacted
        if expected_value:  # Skip empty string
            loga.info(f"The value is: {expected_value}")
            assert message_queue.qsize() == 1
            log_message = message_queue.get(block=False).getMessage()
            assert expected_value not in log_message
            assert "********" in log_message

    # Special cases tests
    @pytest.mark.parametrize(
        "value,description",
        [
            ("p@$$w0rd!*&^%", "special characters"),
            ("line1\\nline2\\nline3", "newlines"),
            ("C:\\Program Files\\App\\bin;D:\\Tools", "Windows paths"),
        ],
    )
    def test_redacted_env_special_values(
        self,
        message_queue: SimpleQueue,
        queue_handler: logging.handlers.QueueHandler,
        value: str,
        description: str,
    ) -> None:
        """Test redacted environment variables with special values."""
        # Setup
        callback_mock = MagicMock()
        loga, _, _ = setup_action_filter_test(
            queue_handler=queue_handler,
            callback=callback_mock,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # WHEN
        loga.info(f"openjd_redacted_env: TEST_VAR={value}")

        # THEN
        # Check that the callback was called with the correct parameters
        callback_mock.assert_called_once_with(
            ActionMessageKind.ENV, {"name": "TEST_VAR", "value": value}, False
        )

        # Check that the message in the log is redacted
        assert message_queue.qsize() == 1
        log_record = message_queue.get(block=False).getMessage()
        assert value not in log_record
        assert "********" in log_record

    # JSON format tests
    @pytest.mark.parametrize(
        "json_case,should_succeed",
        [
            # Standard JSON format (properly escaped strings)
            ('openjd_redacted_env: "FOO=BAR"', True),
            ('openjd_redacted_env: "FOO=BAR\\nBAZ"', True),
            ('openjd_redacted_env: "FOO="', True),
        ],
    )
    def test_redacted_env_json_format(
        self,
        message_queue: SimpleQueue,
        queue_handler: logging.handlers.QueueHandler,
        json_case: str,
        should_succeed: bool,
    ) -> None:
        """Test JSON format for redacted environment variables."""
        # Setup
        callback_mock = MagicMock()
        loga, _, _ = setup_action_filter_test(
            queue_handler=queue_handler,
            callback=callback_mock,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # WHEN
        loga.info(json_case)

        # THEN
        if should_succeed:
            # At least one call should be for setting an environment variable
            env_calls = [
                call
                for call in callback_mock.call_args_list
                if call[0][0] == ActionMessageKind.ENV and isinstance(call[0][1], dict)
            ]
            assert len(env_calls) > 0, f"Case '{json_case}': No environment variable was set"

            # Check that the message in the log is redacted
            assert message_queue.qsize() == 1
            log_record = message_queue.get(block=False).getMessage()

            # Extract the value that should be redacted
            if "\\n" in json_case:
                # For the line break case, check that both parts are redacted
                assert "BAR" not in log_record, f"Value 'BAR' was not redacted in: {log_record}"
                assert "BAZ" not in log_record, f"Value 'BAZ' was not redacted in: {log_record}"
                assert "********" in log_record, f"Redaction marker not found in: {log_record}"
            elif "FOO=BAR" in json_case:
                assert "BAR" not in log_record, f"Value 'BAR' was not redacted in: {log_record}"
                assert "********" in log_record, f"Redaction marker not found in: {log_record}"
            # For empty value case, we don't check for redaction marker since there's nothing to redact
        else:
            # No calls should be for setting an environment variable
            env_calls = [
                call
                for call in callback_mock.call_args_list
                if call[0][0] == ActionMessageKind.ENV and isinstance(call[0][1], dict)
            ]
            assert (
                len(env_calls) == 0
            ), f"Case '{json_case}': Environment variable was set unexpectedly"

    # Consistency tests between openjd_env and openjd_redacted_env
    @pytest.mark.parametrize(
        "case,should_succeed",
        [
            # Success cases - standard format
            ("openjd_env: KEY=VALUE", True),
            ("openjd_env: KEY= VALUE", True),
            ("openjd_env: KEY=VALUE=MORE", True),
            ("openjd_env: KEY=", True),
            # Success cases - quoted format
            ('openjd_env: "FOO=12\\n34"', True),
            ('openjd_env: "FOO="', True),
            # Success cases - JSON format (pre-encoded as strings)
            ('openjd_env: "FOO=BAR"', True),
            ('openjd_env: "FOO=BAR\\nBAZ"', True),
            # Success case - whitespace after prefix
            ("openjd_env:  \t foo=bar", True),
            # Failure cases
            ("openjd_env: KEY =VALUE", False),
            ("openjd_env: KEYVALUE", False),
            ("openjd_env: 1F_F_12=bar", False),
            ("openjd_env: F😁=bar", False),
            # Format issue cases
            ("openjd_env:foo=bar", False),
            ("OPENJD_ENV: foo=bar", False),
            (" openjd_env: foo=bar", False),
        ],
    )
    def test_env_redacted_env_consistency(
        self,
        message_queue: SimpleQueue,
        queue_handler: logging.handlers.QueueHandler,
        case: str,
        should_succeed: bool,
    ) -> None:
        """Test that openjd_redacted_env behaves the same as openjd_env for setting environment variables,
        except for the redaction behavior."""

        # Create a fresh filter and mock for each test case
        callback_mock_env = MagicMock()
        loga_env, _, _ = setup_action_filter_test(
            queue_handler=queue_handler,
            callback=callback_mock_env,
        )

        callback_mock_redacted = MagicMock()
        loga_redacted, _, _ = setup_action_filter_test(
            queue_handler=queue_handler,
            callback=callback_mock_redacted,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # Run both commands with the same input
        loga_env.info(case)
        # Replace "openjd_env" with "openjd_redacted_env" in the command
        redacted_case = case.replace("openjd_env", "openjd_redacted_env")
        redacted_case = redacted_case.replace("OPENJD_ENV", "OPENJD_REDACTED_ENV")
        loga_redacted.info(redacted_case)

        # Clear the queue
        while not message_queue.empty():
            message_queue.get()

        if should_succeed:
            # For success cases, verify both set the environment variable correctly
            callback_mock_env.assert_called_once()
            callback_mock_redacted.assert_called_once()

            # Check that the parameters match
            env_args = callback_mock_env.call_args[0]
            redacted_args = callback_mock_redacted.call_args[0]

            # The first argument should be ActionMessageKind.ENV for both
            assert (
                env_args[0] == redacted_args[0] == ActionMessageKind.ENV
            ), f"Case '{case}': Different message kinds"

            # The second argument should be the same dictionary (name and value)
            assert (
                env_args[1] == redacted_args[1]
            ), f"Case '{case}': Different environment variable settings"

            # The third argument should be False for both
            assert (
                env_args[2] == redacted_args[2] and not env_args[2]
            ), f"Case '{case}': Different third argument"
        else:
            # For failure cases, verify neither sets an environment variable with a dict
            env_success_calls = [
                call
                for call in callback_mock_env.call_args_list
                if call[0][0] == ActionMessageKind.ENV and isinstance(call[0][1], dict)
            ]
            redacted_success_calls = [
                call
                for call in callback_mock_redacted.call_args_list
                if call[0][0] == ActionMessageKind.ENV and isinstance(call[0][1], dict)
            ]

            assert (
                len(env_success_calls) == 0
            ), f"Case '{case}': openjd_env should not set environment variable"
            assert (
                len(redacted_success_calls) == 0
            ), f"Case '{case}': openjd_redacted_env should not set environment variable"

    # Additional tests for specific behaviors
    def test_subsequent_redaction(
        self, message_queue: SimpleQueue, queue_handler: logging.handlers.QueueHandler
    ) -> None:
        """Test that values are redacted in subsequent logs."""
        # Setup
        callback_mock = MagicMock()
        loga, _, _ = setup_action_filter_test(
            queue_handler=queue_handler,
            callback=callback_mock,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # WHEN
        # First set the environment variable
        loga.info("openjd_redacted_env: API_KEY=abcdef123456")

        # Clear the queue
        while not message_queue.empty():
            message_queue.get()

        # Then log a message containing the secret value
        loga.info("Using API key: abcdef123456")

        # THEN
        # Check that the secret value is redacted in the subsequent log
        assert message_queue.qsize() == 1
        log_record = message_queue.get(block=False).getMessage()
        assert "abcdef123456" not in log_record
        assert "Using API key: ********" in log_record

    def test_redaction_persists_after_unset(
        self, message_queue: SimpleQueue, queue_handler: logging.handlers.QueueHandler
    ) -> None:
        """Test that when we unset a redacted environment variable:
        1. The variable is unset (via callback)
        2. The value continues to be redacted in logs"""
        # Setup
        callback_mock = MagicMock()
        loga, _, _ = setup_action_filter_test(
            queue_handler=queue_handler,
            callback=callback_mock,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # Set up redaction
        loga.info("openjd_redacted_env: SECRETVAR=SECRETVAL")

        # Clear the queue of the setup messages
        while not message_queue.empty():
            message_queue.get()

        # WHEN - Unset the variable
        loga.info("openjd_unset_env: SECRETVAR")

        # THEN - The callback should be called to unset the var
        callback_mock.assert_called_with(ActionMessageKind.UNSET_ENV, "SECRETVAR", False)

        # Clear the queue of the unset message
        while not message_queue.empty():
            message_queue.get()

        # AND - The value should still be redacted in logs
        loga.info("The value is: SECRETVAL")
        assert message_queue.qsize() == 1
        log_message = message_queue.get(block=False).getMessage()
        assert "The value is: ********" in log_message
        assert "SECRETVAL" not in log_message

    def test_redacted_env_with_linebreak(
        self, message_queue: SimpleQueue, queue_handler: logging.handlers.QueueHandler
    ) -> None:
        """Test that values with actual line breaks are properly redacted in subsequent logs."""
        # Setup
        callback_mock = MagicMock()
        loga, _, _ = setup_action_filter_test(
            queue_handler=queue_handler,
            callback=callback_mock,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # WHEN - First set the environment variable with a line break using JSON format
        loga.info('openjd_redacted_env: "SECRETVAR2=line\\nbreak"')

        # Clear the queue
        while not message_queue.empty():
            message_queue.get()

        # Then log a message containing the secret value split across lines
        loga.info("We set SECRETVAR2 to line\nbreak")

        # THEN
        # Check that both parts of the secret value are redacted in the subsequent log
        assert message_queue.qsize() == 1
        log_record = message_queue.get(block=False).getMessage()

        assert "line" not in log_record, "First part of the secret value was not redacted"
        assert "break" not in log_record, "Second part of the secret value was not redacted"

    def test_redacted_env_with_multiline_redaction(
        self, message_queue: SimpleQueue, queue_handler: logging.handlers.QueueHandler
    ) -> None:
        """Test that subsequent lines of a multi-line secret are properly redacted."""
        # Setup
        callback_mock = MagicMock()
        loga, _, _ = setup_action_filter_test(
            queue_handler=queue_handler,
            callback=callback_mock,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # WHEN - First set the environment variable with a multi-line value
        loga.info('openjd_redacted_env: "SECRETVAR=first_line\\nsecond_line\\nthird_line"')

        # Clear the queue
        while not message_queue.empty():
            message_queue.get()

        # Then log messages containing the individual lines
        loga.info("The first part is: first_line")

        # THEN
        # Check that the first line is redacted as part of a larger string
        assert message_queue.qsize() == 1
        log_record = message_queue.get(block=False).getMessage()
        assert "first_line" not in log_record, "First part of the secret value was not redacted"
        assert "The first part is: ********" in log_record

        # Log just the second line by itself (should be fully redacted)
        loga.info("second_line")

        # Check that the second line is completely redacted
        assert message_queue.qsize() == 1
        log_record = message_queue.get(block=False).getMessage()
        assert log_record == "********", "Second line was not fully redacted"

        # Log the third line by itself
        loga.info("third_line")

        # Check that the third line is completely redacted
        assert message_queue.qsize() == 1
        log_record = message_queue.get(block=False).getMessage()
        assert log_record == "********", "Third line was not fully redacted"

        # Log the second line with some prefix text (should NOT be fully redacted)
        loga.info("Prefix second_line")

        # Check that the "second_line" part is NOT redacted at all in "Prefix second_line"
        # since it's only in _redacted_lines (exact match only) and not in _redacted_values
        assert message_queue.qsize() == 1
        log_record = message_queue.get(block=False).getMessage()
        assert "Prefix second_line" in log_record, "Line was incorrectly redacted"

    def test_redacted_env_with_multiline_redaction_last_part(
        self, message_queue: SimpleQueue, queue_handler: logging.handlers.QueueHandler
    ) -> None:
        """Test that the last part of a multi-line secret is properly redacted."""
        # Setup
        callback_mock = MagicMock()
        loga, _, _ = setup_action_filter_test(
            queue_handler=queue_handler,
            callback=callback_mock,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # WHEN - First set the environment variable with a multi-line value
        loga.info('openjd_redacted_env: "SECRETVAR=first_line\\nmiddle_line\\nlast_line"')

        # Clear the queue
        while not message_queue.empty():
            message_queue.get()

        # Log the last line with some prefix text (should be partially redacted)
        loga.info("Prefix last_line")

        # THEN
        # Check that the last line is redacted even with a prefix
        assert message_queue.qsize() == 1
        log_record = message_queue.get(block=False).getMessage()
        assert "last_line" not in log_record, "Last part of the secret value was not redacted"
        assert "Prefix ********" in log_record, "Last part was not properly redacted"

        # Log the middle line with some prefix text (should NOT be redacted)
        loga.info("Prefix middle_line")

        # Check that the middle line is NOT redacted when it has a prefix
        assert message_queue.qsize() == 1
        log_record = message_queue.get(block=False).getMessage()
        assert "Prefix middle_line" in log_record, "Middle line was incorrectly redacted"
