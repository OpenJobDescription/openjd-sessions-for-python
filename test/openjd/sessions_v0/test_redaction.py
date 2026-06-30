# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the redaction functionality in the action filter."""

import logging
import logging.handlers
from queue import SimpleQueue

from openjd.sessions._action_filter import (
    ActionMonitoringFilter,
    ActionMessageKind,
    redact_openjd_redacted_env_requests,
)
from openjd.model import RevisionExtensions, SpecificationRevision

from .conftest import setup_action_filter_test


def test_redact_openjd_redacted_env_requests():
    """Test that redact_openjd_redacted_env_requests correctly redacts sensitive information in command strings."""
    # Test command without redaction needed
    command = "echo hello world"
    assert redact_openjd_redacted_env_requests(command) == command

    # Test command with redacted env
    command = "python -c \"print('openjd_redacted_env: PASSWORD=secret123')\""
    assert (
        redact_openjd_redacted_env_requests(command)
        == "python -c \"print('openjd_redacted_env: ********"
    )

    # Test command with multiple redacted env values
    command = (
        'echo "openjd_redacted_env: PASSWORD=secret123"; echo "openjd_redacted_env: API_KEY=abc123"'
    )
    assert redact_openjd_redacted_env_requests(command) == 'echo "openjd_redacted_env: ********'


def test_redaction_with_string_formatting():
    """Test that redaction works correctly with string formatting."""
    # Create a list to capture callback calls
    callback_calls = []

    def callback(kind: ActionMessageKind, message: str, cancel: bool):
        callback_calls.append((kind, message, cancel))

    # Create a RevisionExtensions with REDACTED_ENV_VARS enabled
    revision_extensions = RevisionExtensions(
        spec_rev=SpecificationRevision.v2023_09, supported_extensions=["REDACTED_ENV_VARS"]
    )

    # Create the filter
    action_filter = ActionMonitoringFilter(
        session_id="test_session", callback=callback, revision_extensions=revision_extensions
    )

    # Add a value to redact via redacted_env message
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="openjd_redacted_env: PASSWORD=secret123",
        args=(),
        exc_info=None,
    )
    record.session_id = "test_session"
    action_filter.filter(record)

    # Test string formatting with args
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        msg="Command: %s",
        args=("echo secret123",),
        pathname="",
        lineno=0,
        exc_info=None,
    )
    record.session_id = "test_session"
    action_filter.filter(record)

    # Verify redaction happened after string formatting
    assert record.msg == "Command: echo ********"
    assert not record.args  # Args should be cleared after formatting

    # Test multiple args
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        msg="First: %s, Second: %s",
        args=("secret123", "hello"),
        pathname="",
        lineno=0,
        exc_info=None,
    )
    record.session_id = "test_session"
    action_filter.filter(record)

    assert record.msg == "First: ********, Second: hello"
    assert not record.args

    # Test with non-string args
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        msg="Number: %d, Secret: %s",
        args=(42, "secret123"),
        pathname="",
        lineno=0,
        exc_info=None,
    )
    record.session_id = "test_session"
    action_filter.filter(record)

    assert record.msg == "Number: 42, Secret: ********"
    assert not record.args


class TestRedactionCore:
    """Tests for the core redaction functionality."""

    def test_redaction_preserves_spaces(
        self, message_queue: SimpleQueue, queue_handler: logging.handlers.QueueHandler
    ) -> None:
        """Test that when redacting values in an f-string, spaces around the value are preserved."""
        # Setup
        loga, _, _ = setup_action_filter_test(
            queue_handler=queue_handler,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # Set up redaction
        loga.info("openjd_redacted_env: SECRETVAR=SECRETVAL")

        # Clear the queue of the setup messages
        while not message_queue.empty():
            message_queue.get()

        # WHEN - Message with token
        loga.info("SECRETVAR is . SECRETVAL ;")

        # THEN - The spaces should be preserved in the redacted output
        assert message_queue.qsize() == 1
        log_message = message_queue.get(block=False).getMessage()
        assert "SECRETVAR is . ******** ;" in log_message  # Spaces should be preserved
        assert "SECRETVAL" not in log_message

    def test_overlapping_redactions(
        self, message_queue: SimpleQueue, queue_handler: logging.handlers.QueueHandler
    ) -> None:
        """Test that overlapping redactions are handled correctly."""
        # Setup
        loga, _, _ = setup_action_filter_test(
            queue_handler=queue_handler,
            enabled_extensions=["REDACTED_ENV_VARS"],
        )

        # Test case 1: Overlapping redactions at boundary
        loga.info("openjd_redacted_env: KEY1=FOOOBAR")
        loga.info("openjd_redacted_env: KEY2=BARKEY")

        # Clear the queue of the setup messages
        while not message_queue.empty():
            message_queue.get()

        # Log a message containing the overlapping string
        loga.info("The value is: FOOOBARKEY")

        # The entire overlapping string should be redacted
        assert message_queue.qsize() == 1
        log_message = message_queue.get(block=False).getMessage()
        assert "The value is: ********" in log_message
        assert "FOOOBARKEY" not in log_message

        # Test case 2: One redaction completely contained within another
        loga.info("openjd_redacted_env: KEY3=SUPERSECRETPASSWORD")
        loga.info("openjd_redacted_env: KEY4=SECRET")

        # Clear the queue of the setup messages
        while not message_queue.empty():
            message_queue.get()

        # Log a message containing the nested redaction
        loga.info("The value is: SUPERSECRETPASSWORD")

        # The entire string should be redacted with a single redaction
        assert message_queue.qsize() == 1
        log_message = message_queue.get(block=False).getMessage()
        assert "The value is: ********" in log_message
        assert "SUPERSECRETPASSWORD" not in log_message
